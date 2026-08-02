# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 James Braid
"""Driver for the `unifi-emu-herder` child process.

The herder is a foreground process that starts fake UniFi devices as Docker
containers on a network this harness already owns. The ownership split is the
point of it: the herder plans devices and owns their container lifecycle, and
nothing else. It never starts a controller, never takes credentials, and never
creates or removes our network — so adoption and every assertion stay here
(see adopt.py).

Its supported surface is the protocol-1 NDJSON stream on stdout, nothing more.
Diagnostics and aggregated device logs go to stderr and are evidence only:
parsing them, or inferring which runtime backed a device, is off-contract.

Two mechanics below are load-bearing rather than stylistic:

* stdout and stderr are drained CONTINUOUSLY, by threads, for the whole
  lifetime of the child. A device fleet writes far more than a pipe buffer
  holds; a driver that reads only when it wants an event wedges the child
  behind a full pipe and then blames the timeout.
* The process is never killed to end a run. SIGKILL skips the herder's own
  cleanup, which is the only thing that removes the device containers it
  made. Teardown sends SIGTERM and waits for the terminal `stopped`; the
  kill is a last resort after the stop deadline, and it is reported.
"""
import contextlib
import json
import os
import queue
import signal
import subprocess
import threading
import time
import warnings
from collections.abc import Iterator
from dataclasses import dataclass

from . import pins
from .support import RunningController, unavailable

# The one stdout control-protocol version this driver understands. A higher
# one is a different contract, not a superset: fields may be added within a
# version, so unknown fields are ignored, but an unknown version is fatal.
PROTOCOL_VERSION = 1

# Where the runner's herder binary and (development builds only) its
# synthetic device image come from. UNIFI_TEST_*, like every other knob this
# harness owns: UNIFI_EMU_* is the herder's own namespace for the variables
# it hands a device container, and borrowing it here would read as part of
# that contract. Same names go-unifi's fixture uses.
HERDER_BIN_ENV = "UNIFI_TEST_HERDER_BIN"
SYNTHETIC_IMAGE_ENV = "UNIFI_TEST_HERDER_SYNTHETIC_IMAGE"

# Every fixture-side wait is strictly longer than the child's own deadline
# for the same phase, so a stuck run is reported by the herder as the failure
# it is — with a code and a phase — instead of by this side as an unexplained
# timeout.
_SLACK_S = 30.0

# How long to keep reading after the child's streams close, so a terminal
# event written just before exit is never missed.
_DRAIN_GRACE_S = 2.0


class HerderError(Exception):
    """The herder failed, or broke its own protocol."""


class HerderTimeout(HerderError):
    """An expected event never arrived. Distinct because a teardown that
    times out has to be killed, and a killed herder leaks containers."""


@dataclass(frozen=True)
class HerderDevice:
    """One device's public identity plus the address Docker gave it.

    These are the only identities that cross the boundary: no container ids,
    no runtime detail. Devices batched into one container intentionally SHARE
    an ip, so ip is never device identity — the controller keys on mac.
    """
    index: int
    model: str
    mac: str
    serial: str
    name: str
    ip: str


def _duration(seconds: float) -> str:
    """Go duration text, which is what the herder's flags parse."""
    return f"{seconds:g}s"


def herder_argv(
    binary: str,
    *,
    network: str,
    inform_url: str,
    synthetic_image: str = "",
    startup_timeout_s: float = 300.0,
    stop_timeout_s: float = 30.0,
) -> list[str]:
    """The command line for one run, reading its request from stdin.

    `synthetic_image` is omitted when empty on purpose: a release build
    carries a version-matched default, and passing an empty override would
    take that away. A development build has no default and needs one.
    """
    argv = [
        binary,
        "--network", network,
        "--inform-url", inform_url,
        "--devices", "-",
        "--startup-timeout", _duration(startup_timeout_s),
        "--stop-timeout", _duration(stop_timeout_s),
    ]
    if synthetic_image:
        argv += ["--synthetic-image", synthetic_image]
    return argv


def _device(entry: dict) -> HerderDevice:
    try:
        return HerderDevice(
            index=int(entry["index"]), model=str(entry["model"]),
            mac=str(entry["mac"]), serial=str(entry["serial"]),
            name=str(entry["name"]), ip=str(entry["ip"]),
        )
    except KeyError as exc:
        raise HerderError(f"ready device entry is missing {exc}: {entry!r}") from exc


class Herder:
    """One herder run: start, wait for ready, stop.

    Construct with a full argv (see herder_argv) so tests can drive the same
    state machine with a stand-in binary.
    """

    def __init__(self, argv: list[str], request: dict, *, env: dict | None = None) -> None:
        self._argv = list(argv)
        self._request = request
        self._env = env
        self._proc: subprocess.Popen | None = None
        self._events: queue.Queue = queue.Queue()
        self._stderr: list[str] = []
        self._threads: list[threading.Thread] = []
        self._terminal: dict | None = None

    # --- lifecycle ----------------------------------------------------

    def start(self) -> None:
        self._proc = subprocess.Popen(  # noqa: S603 - argv is built, never a shell string
            self._argv,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, bufsize=1, env=self._env,
        )
        # Drain before writing: the herder emits `started` before it even
        # decodes the request, so a driver that writes first and reads later
        # can already be behind.
        self._spawn(self._drain_stdout)
        self._spawn(self._drain_stderr)
        assert self._proc.stdin is not None
        try:
            self._proc.stdin.write(json.dumps(self._request))
        except BrokenPipeError:  # the child died before reading; wait_ready reports why
            pass
        finally:
            # The request is exactly one document and ends at EOF. An open
            # stdin means the herder is still waiting for the rest of it.
            self._proc.stdin.close()

    def wait_started(self, timeout_s: float) -> str:
        """Block until `started` and return the run id.

        On a short clock of its own: `started` precedes even request
        decoding, so a binary that has not said it within seconds is not the
        herder. Waiting out the whole startup budget for that would turn a
        mistyped path into minutes of silence.
        """
        return str(self._await(timeout_s, want="started").get("run_id", ""))

    def wait_ready(self, timeout_s: float) -> list[HerderDevice]:
        """Block until `ready` and return the resolved identities."""
        event = self._await(timeout_s, want="ready")
        return [_device(entry) for entry in event.get("devices", [])]

    def stop(self, timeout_s: float) -> None:
        """SIGTERM, require the terminal `stopped`, wait for the process."""
        proc = self._require_started()
        if proc.poll() is None:
            proc.send_signal(signal.SIGTERM)
        try:
            self._await(timeout_s, want="stopped")
        except HerderTimeout as exc:
            self.kill()
            raise HerderError(
                f"herder did not stop within {timeout_s}s of SIGTERM and was "
                f"killed — its cleanup never ran, so device containers may "
                f"remain: {exc}"
            ) from exc
        except HerderError:
            self.kill()
            raise
        # The herder confirms every device container is gone before it emits
        # `stopped`, so the remaining wait is only the process itself.
        try:
            proc.wait(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            self.kill()
            raise HerderError(
                f"herder emitted `stopped` but did not exit within {timeout_s}s"
                f"{self._evidence()}"
            ) from None
        self._join()

    def kill(self) -> None:
        """Last resort: SIGKILL and reap. Leaks containers — always report it."""
        proc = self._proc
        if proc is None:
            return
        if proc.poll() is None:
            proc.kill()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:  # pragma: no cover - unkillable child
            pass
        self._join()

    @property
    def returncode(self) -> int | None:
        return None if self._proc is None else self._proc.returncode

    @property
    def stderr_text(self) -> str:
        return "".join(self._stderr)

    # --- the protocol state machine -----------------------------------

    def _await(self, timeout_s: float, *, want: str) -> dict:
        """Consume events until `want` arrives, or the run ends without it."""
        if self._terminal is not None:
            return self._terminal_or_raise(self._terminal, want)
        deadline = time.monotonic() + timeout_s
        skipped: dict = {}
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                detail = ""
                if skipped:
                    versions = ", ".join(f"protocol {v}" for v in sorted(skipped, key=str))
                    detail = f" (skipped {sum(skipped.values())} event(s) at {versions})"
                raise HerderTimeout(
                    f"no `{want}` event within {timeout_s}s{detail}{self._evidence()}"
                )
            try:
                kind, payload = self._events.get(timeout=min(remaining, 0.5))
            except queue.Empty:
                continue
            if kind == "other_protocol":
                skipped[payload] = skipped.get(payload, 0) + 1
                continue
            if kind == "malformed":
                raise HerderError(
                    f"herder wrote non-protocol output on stdout: {payload!r}"
                    f"{self._evidence()}"
                )
            if kind == "eof":
                raise HerderError(self._ended_without(want))
            event = payload
            name = event.get("event")
            if name == want:
                if name in ("stopped", "failed"):
                    self._terminal = event
                return event
            if name in ("stopped", "failed"):
                self._terminal = event
                return self._terminal_or_raise(event, want)
            # `started`, or a field-compatible event added later: not ours.

    def _terminal_or_raise(self, event: dict, want: str) -> dict:
        if event.get("event") == want:
            return event
        if event.get("event") == "failed":
            raise HerderError(
                f"herder failed in phase {event.get('phase')!r} with code "
                f"{event.get('code')!r}: {event.get('message')} "
                f"(cleanup_complete={event.get('cleanup_complete')})"
                f"{self._evidence()}"
            )
        raise HerderError(
            f"herder stopped before `{want}` (reason {event.get('reason')!r})"
            f"{self._evidence()}"
        )

    def _ended_without(self, want: str) -> str:
        proc = self._require_started()
        try:
            proc.wait(timeout=_DRAIN_GRACE_S)
        except subprocess.TimeoutExpired:  # pragma: no cover - streams closed, process live
            pass
        return (
            f"herder exited with exit {proc.returncode} before any terminal "
            f"event (waiting for `{want}`){self._evidence()}"
        )

    def _evidence(self) -> str:
        text = self.stderr_text.strip()
        if not text:
            return ""
        # Aggregated device logs are the only account of why a fleet never
        # came up, so they travel with the failure rather than the run log.
        return "\n--- herder stderr ---\n" + text[-8000:]

    # --- plumbing -----------------------------------------------------

    def _require_started(self) -> subprocess.Popen:
        if self._proc is None:
            raise HerderError("herder was never started")
        return self._proc

    def _spawn(self, target) -> None:
        thread = threading.Thread(target=target, daemon=True)
        thread.start()
        self._threads.append(thread)

    def _join(self) -> None:
        for thread in self._threads:
            thread.join(timeout=_DRAIN_GRACE_S)

    def _drain_stdout(self) -> None:
        proc = self._require_started()
        assert proc.stdout is not None
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except ValueError:
                self._events.put(("malformed", line))
                continue
            if not isinstance(event, dict):
                self._events.put(("malformed", line))
                continue
            if event.get("protocol") != PROTOCOL_VERSION:
                # A different version is not this driver's to interpret. It
                # is skipped rather than refused, so a herder that ever
                # carries both stays readable — but it is recorded, because
                # a wait that then finds nothing must explain itself.
                self._events.put(("other_protocol", event.get("protocol")))
                continue
            self._events.put(("event", event))
        self._events.put(("eof", None))

    def _drain_stderr(self) -> None:
        proc = self._require_started()
        assert proc.stderr is not None
        for line in proc.stderr:
            self._stderr.append(line)


def resolve_binary() -> str:
    """The herder binary, or a skip-vs-fail when the runner has none.

    The path stays env-resolved because a filesystem location is not
    something pins.py can own; the VERSION it must report is pinned, and
    binary_version checks it.
    """
    binary = os.environ.get(HERDER_BIN_ENV, "").strip()
    if not binary or not os.access(binary, os.X_OK):
        unavailable(
            f"{HERDER_BIN_ENV} is unset or not executable ({binary!r}) — install "
            f"unifi-emu-herder {pins.EMU_VERSION} (see ci/install-herder.sh)"
        )
    return binary


def binary_version(binary: str) -> str:
    """What the herder reports for --version. "dev" for an unreleased build."""
    try:
        done = subprocess.run(  # noqa: S603 - argv, never a shell string
            [binary, "--version"], capture_output=True, text=True, timeout=30,
        )
    except OSError as exc:
        raise HerderError(f"cannot run {binary} --version: {exc}") from exc
    if done.returncode != 0:
        raise HerderError(
            f"{binary} --version exited {done.returncode}: {done.stderr.strip()!r}"
        )
    return done.stdout.strip()


def synthetic_image_for(version: str, override: str) -> str:
    """The --synthetic-image to pass a herder reporting `version`, or "".

    A release build compiles in the image built from its own tag, so the
    correct thing to pass it is nothing — that is what keeps the binary and
    the image from ever disagreeing. A development build has no default and
    needs one supplied.

    A binary reporting some other release is drift, and drift fails rather
    than warns: the protocol and the flags come from the binary, so a stale
    one means this suite is testing an emulator the pin does not describe.
    An override does not excuse it, because the image is only half of what
    the binary decides.
    """
    if version != pins.EMU_VERSION and version != "dev":
        raise HerderError(
            f"herder reports version {version!r}, but pins.EMU_VERSION is "
            f"{pins.EMU_VERSION!r} — install the pinned release, or bump the pin"
        )
    if override:
        return override
    if version == "dev":
        unavailable(
            f"a dev herder build compiles in no synthetic image, so "
            f"{SYNTHETIC_IMAGE_ENV} must name one (a {pins.EMU_VERSION} release "
            f"build needs neither)"
        )
    return ""


def run_fleet(
    controller: RunningController,
    request: dict,
    *,
    startup_timeout_s: float = 300.0,
    stop_timeout_s: float = 60.0,
) -> Iterator[tuple[RunningController, list[HerderDevice]]]:
    """Run one herder against a controller and yield its ready identities.

    Yields only after `ready`. Teardown sends SIGTERM, requires the terminal
    `stopped` and waits for the process, so every device container the herder
    made is gone before the controller and network are torn down.

    A controller that cannot host devices — no network, or no inform URL —
    is a skip, never a fabricated network: an externally managed controller
    may use the herder only if its fixture can supply both.
    """
    if not (controller.network and controller.inform_url):
        unavailable(
            "this controller exposes no docker network and inform URL, so it "
            "cannot host a device fleet"
        )
    binary = resolve_binary()
    image = synthetic_image_for(
        binary_version(binary), os.environ.get(SYNTHETIC_IMAGE_ENV, "").strip()
    )
    herder = Herder(
        herder_argv(
            binary, network=controller.network, inform_url=controller.inform_url,
            synthetic_image=image, startup_timeout_s=startup_timeout_s,
            stop_timeout_s=stop_timeout_s,
        ),
        request,
        env=child_env(),
    )
    def teardown() -> None:
        if os.environ.get("UNIFI_TEST_KEEP"):
            # Keeping the fleet means keeping the herder: it is the only
            # thing that removes those containers.
            warnings.warn(
                "UNIFI_TEST_KEEP set — leaving the herder running with its "
                "devices; it is the only thing that removes those containers",
                stacklevel=2,
            )
            return
        # The herder's own --stop-timeout plus its cleanup slack, so a
        # force-remove still has room before this gives up on it.
        herder.stop(timeout_s=stop_timeout_s + _SLACK_S)

    herder.start()
    try:
        herder.wait_started(timeout_s=_SLACK_S)
        devices = herder.wait_ready(timeout_s=startup_timeout_s + _SLACK_S)
        yield controller, devices
    except BaseException:
        # A run that already failed still has to be torn down, but the
        # failure that got us here names the code and phase; a second one
        # raised out of cleanup would bury it.
        with contextlib.suppress(HerderError):
            teardown()
        raise
    else:
        teardown()


def child_env() -> dict:
    """The environment the herder child needs, derived from ours.

    The herder resolves Docker through Testcontainers' own configuration, and
    that does not read `docker context`. On a context-based engine (colima,
    Docker Desktop) an unset DOCKER_HOST leaves the child pointing at a socket
    path that does not exist, and it dies before it creates anything.

    Nothing here disables the reaper: the herder refuses to run when the
    effective Testcontainers configuration has it off, because crash cleanup
    is what removes device containers when the herder is killed.
    """
    env = dict(os.environ)
    env.pop("TESTCONTAINERS_RYUK_DISABLED", None)
    if not env.get("DOCKER_HOST"):
        host = _docker_host()
        if host:
            env["DOCKER_HOST"] = host
    return env


def _docker_host() -> str:
    """The daemon endpoint this harness itself is talking to."""
    try:
        from testcontainers.core.docker_client import DockerClient
        return str(DockerClient().client.api.base_url)
    except Exception:  # noqa: BLE001 - no docker is the caller's problem, not ours
        return ""
