# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 James Braid
"""Flavor definitions and container/URL-mode boot for controller fixtures.

Skip-vs-fail per the contract: missing docker or URL env is a friendly
skip locally; with UNIFI_TEST_REQUIRE set (CI always sets it) the same
condition is a hard failure — no skip may satisfy a required check.
"""
import ipaddress
import os
import sys
import urllib.parse
import warnings
from collections.abc import Iterator
from dataclasses import dataclass

import pytest

from . import pins
from .readiness import ReadinessError, wait_ready

# The classic Network App's inform listener. Devices POST their inform here;
# it is not the API port and is never published to the host.
INFORM_PORT = 8080


@dataclass(frozen=True)
class Flavor:
    name: str            # "seeded" | "sim" | "uos"
    image: str
    url_env: str         # UNIFI_TEST_<FLAVOR>_URL
    image_env: str       # UNIFI_TEST_<FLAVOR>_IMAGE
    username: str
    password: str
    port: int            # controller API port inside the container
    boot_timeout_s: float
    scheme: str = "https"  # base_url scheme for `port`, in container mode
    inform_port: int = INFORM_PORT
    # URL mode only: an external controller has no container to inspect, so
    # the operator supplies both halves or neither (see boot_flavor).
    network_env: str = ""      # UNIFI_TEST_<FLAVOR>_NETWORK
    inform_env: str = ""       # UNIFI_TEST_<FLAVOR>_INFORM_URL
    # Seeded UOS only: the file inside the container where the boot publishes a
    # working X-API-KEY (its healthcheck gates on the key, so a healthy
    # container has it). boot_flavor reads it (container mode) or takes it from
    # key_env (URL mode). Empty for flavors that mint no key.
    api_key_file: str = ""
    key_env: str = ""    # UNIFI_TEST_<FLAVOR>_KEY, for URL mode


@dataclass(frozen=True)
class RunningController:
    base_url: str
    username: str
    password: str
    site: str
    external: bool  # True in URL mode — never assert pin-derived facts then
    # UOS-only: the unifi-os dialect endpoint (443, /proxy/network +
    # X-API-KEY). Empty for non-UOS flavors and whenever URL mode has no
    # UNIFI_TEST_UOS_NATIVE_URL — callers must treat empty as unavailable.
    native_url: str = ""
    # The Docker network this controller sits on, and the inform endpoint a
    # container on that network can reach it at. Both are set together in
    # container mode; in URL mode both are set only when the operator
    # supplied them. Empty means "cannot host devices" — never fake either
    # one to make a code path run.
    network: str = ""
    inform_url: str = ""
    # Seeded UOS only: the baked X-API-KEY for the production unifi-os dialect.
    # Empty when the flavor mints none, or URL mode left key_env unset.
    api_key: str = ""


def inform_url(ip: str, port: int) -> str:
    """The one inform endpoint form the herder accepts.

    Exactly http://<canonical-IPv4-literal>:<port>/inform. Device containers
    resolve nothing and the controller rejects an inform whose host is not an
    address it recognizes, so a hostname, an IPv6 literal, a loopback address
    or a non-canonical spelling all produce a fleet that starts cleanly and
    then never adopts. Refusing here names the problem while it is still one
    line of fixture configuration.
    """
    try:
        parsed = ipaddress.IPv4Address(ip)
    except ipaddress.AddressValueError as exc:
        raise ValueError(f"inform host {ip!r} is not a canonical IPv4 literal: {exc}") from exc
    if parsed.is_loopback or parsed.is_unspecified:
        raise ValueError(f"inform host {ip!r} is not reachable from a device container")
    return f"http://{parsed}:{port}/inform"


def check_inform_url(url: str) -> str:
    """Validate a whole inform URL, returning it unchanged.

    The complement of inform_url() for the one place the harness does not
    build the URL itself: an externally managed controller, where the
    operator supplies it. Same rules, checked where the operator can still
    see which variable is wrong.
    """
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "http":
        raise ValueError(f"inform URL {url!r} must use http, got {parsed.scheme!r}")
    if parsed.path != "/inform" or parsed.query or parsed.fragment:
        raise ValueError(f"inform URL {url!r} must end at the path /inform, with no query")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError(f"inform URL {url!r} has an unusable port: {exc}") from exc
    if port is None:
        raise ValueError(f"inform URL {url!r} needs an explicit port")
    rebuilt = inform_url(parsed.hostname or "", port)
    if rebuilt != url:
        raise ValueError(f"inform URL {url!r} is not canonical (want {rebuilt!r})")
    return url


def _endpoint_ipv4(attrs: dict, network: str) -> str:
    """The container's address on `network`, from a docker inspection.

    Read back rather than assumed, and selected by name rather than by
    taking whatever comes first. Attaching at create time leaves the
    controller single-homed, which is what makes it advertise this same
    address for inform after adoption; selecting by name keeps that true if
    it ever gains a second attachment.
    """
    networks = attrs.get("NetworkSettings", {}).get("Networks", {})
    if network not in networks:
        raise ValueError(
            f"controller is not attached to network {network!r} "
            f"(attached: {sorted(networks)})"
        )
    ip = str(networks[network].get("IPAddress") or "")
    if not ip:
        raise ValueError(f"controller has no IPv4 address on network {network!r} yet")
    return ip


SEEDED = Flavor(
    name="seeded", image=pins.SEEDED_IMAGE,
    url_env="UNIFI_TEST_SEEDED_URL", image_env="UNIFI_TEST_SEEDED_IMAGE",
    username="admin", password="unifi-containers-seeded",
    port=8443, boot_timeout_s=300,
    network_env="UNIFI_TEST_SEEDED_NETWORK", inform_env="UNIFI_TEST_SEEDED_INFORM_URL",
)
SIM = Flavor(
    name="sim", image=pins.SIM_IMAGE,
    url_env="UNIFI_TEST_SIM_URL", image_env="UNIFI_TEST_SIM_IMAGE",
    username="admin", password="admin",
    port=8443, boot_timeout_s=300,
    network_env="UNIFI_TEST_SIM_NETWORK", inform_env="UNIFI_TEST_SIM_INFORM_URL",
)
UOS_SEEDED = Flavor(
    name="uos-seeded", image=pins.UOS_SEEDED_IMAGE,
    url_env="UNIFI_TEST_UOS_SEEDED_URL", image_env="UNIFI_TEST_UOS_SEEDED_IMAGE",
    username="admin", password="admin",
    port=443, boot_timeout_s=600,
    api_key_file="/unifi/api-key", key_env="UNIFI_TEST_UOS_SEEDED_KEY",
    # The owner-seeded UOS: headless 443 login works (unifi-core /api/setup),
    # real empty site, NO 7443 direct port. base_url is the 443 native API —
    # real nginx-terminated TLS — the production unifi-os dialect surface
    # (/proxy/network + X-API-KEY), which the native round-trip exercises
    # and readiness reads. Deliberately name != "uos" so boot_flavor's
    # is_uos dual-443-expose stays off; it needs UOS_RUN_KWARGS all the same
    # (passed by the fixture) and boots behind the image healthcheck.
    scheme="https",
)
UOS = Flavor(
    name="uos", image=pins.UOS_IMAGE,
    url_env="UNIFI_TEST_UOS_URL", image_env="UNIFI_TEST_UOS_IMAGE",
    username="admin", password="admin",
    port=7443, boot_timeout_s=600,  # image healthcheck start-period is 10 min
    # 7443 is systemd-socket-proxyd fronting the bundled Network App's own
    # 127.0.0.1:8081 — plain HTTP by the image's own entrypoint contract
    # (UOS_NETWORK_DIRECT="Direct (SSO-free) UniFi Network API port").
    # Confirmed empirically during the Task 14 probe: an HTTPS handshake
    # against the mapped port hangs/fails ([SSL: WRONG_VERSION_NUMBER]);
    # plain HTTP gets a clean {"meta":{"rc":"ok"}}. 443 (native_url) is
    # unaffected — that's real nginx-terminated TLS.
    scheme="http",
)

# The documented UOS runtime contract (systemd PID 1): cap list — no
# privileged mode — host cgroupns with /sys/fs/cgroup rw, tmpfs set.
# Canonical: unifi-os/examples/docker-compose.yml in unifi-containers.
UOS_RUN_KWARGS: dict = {
    "cgroupns": "host",
    "cap_drop": ["ALL"],
    "cap_add": [
        "SYS_ADMIN", "NET_ADMIN", "NET_RAW", "NET_BIND_SERVICE",
        "DAC_OVERRIDE", "DAC_READ_SEARCH", "FOWNER", "CHOWN",
        "SETUID", "SETGID", "KILL", "SYS_CHROOT", "SYS_PTRACE",
        "SYS_RESOURCE", "AUDIT_WRITE", "MKNOD",
    ],
    "tmpfs": {
        "/run": "exec", "/run/lock": "", "/tmp": "exec",
        "/var/lib/journal": "", "/var/opt/unifi/tmp": "size=64m",
    },
    "volumes": [("/sys/fs/cgroup", "/sys/fs/cgroup", "rw")],
}


def _report_keep(flavor: Flavor, base_url: str) -> None:
    """Surface UNIFI_TEST_KEEP's "container left running" notice.

    warnings.warn (not print) so it lands in pytest's warnings summary
    unconditionally — a bare print() is swallowed by pytest's output
    capture unless the run passes -s, so the operator would set
    UNIFI_TEST_KEEP and see no confirmation the container was actually
    kept.
    """
    warnings.warn(
        f"UNIFI_TEST_KEEP set — leaving {flavor.name} container running: {base_url}",
        stacklevel=2,
    )


def unavailable(reason: str) -> None:
    """Contract skip-vs-fail knob."""
    if os.environ.get("UNIFI_TEST_REQUIRE"):
        pytest.fail(f"UNIFI_TEST_REQUIRE is set and {reason}", pytrace=False)
    pytest.skip(reason)


def _docker_available() -> bool:
    # Any exception (including hostless-machine construction crashes seen in
    # other language ports) means "unavailable" — explicit selection must
    # report a clean skip/fail, never a stack trace.
    try:
        from testcontainers.core.docker_client import DockerClient
        DockerClient().client.ping()
        return True
    except Exception:  # noqa: BLE001
        return False


def _ensure_vm_socket_override() -> None:
    # testcontainers' Ryuk reaper bind-mounts the client-visible docker
    # socket path into its own container. On macOS every engine is
    # VM-based (colima, Docker Desktop) and that host path does not exist
    # inside the VM — Ryuk dies with "error while creating mount source
    # path". The VM-side socket is /var/run/docker.sock; point
    # testcontainers at it. Preserves the reaper backstop — never disable
    # Ryuk here. Respect an explicit operator override.
    if sys.platform != "darwin" or os.environ.get("TESTCONTAINERS_DOCKER_SOCKET_OVERRIDE"):
        return
    os.environ["TESTCONTAINERS_DOCKER_SOCKET_OVERRIDE"] = "/var/run/docker.sock"


def _release(container: object, network: object) -> None:
    """Stop the container, then remove the network it sat on.

    The order is load-bearing: Docker refuses to remove a network that still
    holds a container. Removal is attempted even when stopping raises, because
    a boot creates a network every time and Docker's default pool holds only a
    handful — skipping removal on the error path leaks one per run until boots
    start failing on address-pool exhaustion.

    A failed removal is reported, never swallowed, but it does not replace the
    exception that caused it: removal usually fails *because* the container is
    still attached, so the container's error is the one worth propagating.
    """
    try:
        container.stop()
    finally:
        try:
            network.remove()
        except Exception as exc:  # noqa: BLE001 - reported, never masking
            warnings.warn(
                f"could not remove docker network {getattr(network, 'name', network)!r}: "
                f"{exc} — it has leaked",
                stacklevel=2,
            )


def _split_tmpfs(run_kwargs: dict) -> tuple[dict, dict]:
    """Separate tmpfs mounts from the rest of a flavor's run kwargs.

    DockerContainer names no `tmpfs` parameter: it initialises self.tmpfs to
    {} and sweeps whatever it does not name into **kwargs, then start() calls
    create(tmpfs=self.tmpfs, **kwargs). A tmpfs passed as a run kwarg
    therefore arrives twice and the container never starts. It has to be
    applied through with_tmpfs_mount, which owns that attribute.

    Copies rather than pops in place: the flavor contracts are module-level
    dicts reused by every boot, and draining one would leave the next boot of
    the session with no mounts at all.
    """
    kwargs = dict(run_kwargs)
    return kwargs, dict(kwargs.pop("tmpfs", {}) or {})


def _external_device_host(flavor: Flavor) -> tuple[str, str]:
    """URL mode's (network, inform URL), or ("", "") when it cannot host devices.

    An external controller has no container to inspect, so the operator
    supplies both halves or neither. Half a pair is a configuration mistake,
    not a fallback: a network with no inform URL would start devices that
    never adopt, and an inform URL with no network has nothing to start them
    on. Nothing here invents a network to make the path run.
    """
    if not (flavor.network_env and flavor.inform_env):
        return "", ""
    network = os.environ.get(flavor.network_env, "").strip()
    supplied = os.environ.get(flavor.inform_env, "").strip()
    if not network and not supplied:
        return "", ""
    if not (network and supplied):
        raise ReadinessError(
            f"{flavor.network_env} and {flavor.inform_env} must be set together "
            f"(got network={network!r}, inform_url={supplied!r})"
        )
    # Validate here, where the operator can see which variable is wrong,
    # rather than as an inform_url_invalid failure inside a child process.
    try:
        check_inform_url(supplied)
    except ValueError as exc:
        raise ReadinessError(f"{flavor.inform_env}: {exc}") from exc
    return network, supplied


def _read_api_key(container: object, path: str) -> str:
    """cat the seeded X-API-KEY out of the running container. The key-baked
    seeded healthcheck gates on the key working (a 200 from /proxy/network), so
    a healthy such container is guaranteed to have published it. An older image
    without the key-baking is the skip-vs-fail case, not a crash — the harness
    contract turns it into a friendly skip locally, a hard failure under
    UNIFI_TEST_REQUIRE."""
    exit_code, output = container.get_wrapped_container().exec_run(["cat", path])
    key = output.decode(errors="replace").strip() if output else ""
    if exit_code != 0 or not key:
        unavailable(
            f"no baked API key at {path} — this seeded UOS image predates the "
            f"key-baking (exec exit {exit_code})"
        )
    return key


def boot_flavor(flavor: Flavor, run_kwargs: dict | None = None) -> Iterator[RunningController]:
    is_uos = flavor.name == "uos"
    url = os.environ.get(flavor.url_env)
    if url:
        base_url = url.rstrip("/")
        wait_ready(base_url, flavor.username, flavor.password,
                   timeout_s=flavor.boot_timeout_s)
        native_url = ""
        if is_uos:
            # No docker container to read a mapped port from in URL mode —
            # the operator must supply the native (443, unifi-os dialect)
            # endpoint directly. Unset means "skip native scenarios".
            native_url = os.environ.get("UNIFI_TEST_UOS_NATIVE_URL", "").rstrip("/")
        network, inform = _external_device_host(flavor)
        # URL mode has no container to read the key file from — the operator
        # supplies it (read once with `docker exec ... cat /unifi/api-key`).
        api_key = os.environ.get(flavor.key_env, "") if flavor.key_env else ""
        yield RunningController(base_url, flavor.username, flavor.password,
                                site="default", external=True, native_url=native_url,
                                network=network, inform_url=inform, api_key=api_key)
        return

    if not _docker_available():
        unavailable(f"docker unavailable and {flavor.url_env} unset")

    _ensure_vm_socket_override()

    from testcontainers.core.container import DockerContainer
    from testcontainers.core.network import Network
    from testcontainers.core.wait_strategies import HealthcheckWaitStrategy

    image = os.environ.get(flavor.image_env, flavor.image)
    kwargs, tmpfs = _split_tmpfs(run_kwargs or {})
    container = DockerContainer(image, **kwargs)
    for path, options in tmpfs.items():
        # The option string is stored verbatim, so "exec" and "size=64m"
        # both survive despite the parameter being named for sizes.
        container = container.with_tmpfs_mount(path, options or None)
    # UOS alone also exposes 443 — the unifi-os dialect (native) endpoint,
    # distinct from the 7443 bundled-network-app port the healthcheck and
    # base_url use. A second port on the other flavors would change their
    # documented contract, so this is UOS-only.
    container = container.with_exposed_ports(flavor.port, 443) if is_uos \
        else container.with_exposed_ports(flavor.port)
    # A user-defined network of our own, so a sibling device fleet can reach
    # the controller's inform port directly. The controller keeps its
    # host-published API ports, so pytest reaches it exactly as before. This
    # fixture owns the network for the same reason it owns the controller:
    # whatever creates it has to outlive every device that joins it.
    network = Network()
    network.create()
    # Everything from here on is inside the handler, not just the start: the
    # network exists from the line above, so any step between it and a running
    # container would otherwise leak one with nothing to catch it.
    try:
        container = container.with_network(network)
        container = container.waiting_for(
            HealthcheckWaitStrategy().with_startup_timeout(int(flavor.boot_timeout_s))
        )
        container.start()
    except Exception as exc:
        tail = ""
        try:
            stdout, stderr = container.get_logs()
            tail = (stdout + stderr).decode(errors="replace")[-4000:]
        except Exception:  # noqa: BLE001
            pass
        # A container that never became healthy still exists: it was created
        # and started, which is why get_logs() above can say anything at all.
        # So it is stopped before the network, exactly as on the happy path —
        # removing a network with a live endpoint on it fails, and then both
        # leak. Failing earlier, before there is a container to stop, lands
        # here too; _release reports that and still removes the network.
        # Either way the readiness failure below, with its log tail, is what
        # explains the boot, so a cleanup error must not displace it.
        try:
            _release(container, network)
        except Exception as release_exc:  # noqa: BLE001 - never mask why the boot failed
            warnings.warn(
                f"could not release the {flavor.name} container after a failed "
                f"boot: {release_exc} — it may have leaked",
                stacklevel=2,
            )
        raise ReadinessError(
            f"{flavor.name} container ({image}) never became healthy: {exc}\n"
            f"--- log tail ---\n{tail}"
        ) from exc

    host = container.get_container_host_ip()
    base_url = f"{flavor.scheme}://{host}:{container.get_exposed_port(flavor.port)}"
    native_url = f"https://{host}:{container.get_exposed_port(443)}" if is_uos else ""
    try:
        wrapped = container.get_wrapped_container()
        wrapped.reload()  # the address is assigned at start, not at create
        inform = inform_url(_endpoint_ipv4(wrapped.attrs, network.name), flavor.inform_port)
        # Read inside the try so a missing-key skip still stops the container.
        api_key = _read_api_key(container, flavor.api_key_file) if flavor.api_key_file else ""
        yield RunningController(base_url, flavor.username, flavor.password,
                                site="default", external=False, native_url=native_url,
                                network=network.name, inform_url=inform, api_key=api_key)
    finally:
        if os.environ.get("UNIFI_TEST_KEEP"):
            _report_keep(flavor, base_url)
        else:
            _release(container, network)
