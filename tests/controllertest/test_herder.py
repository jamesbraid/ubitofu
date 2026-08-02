# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 James Braid
"""Unit tests for the herder process driver (unmarked — no docker needed).

Every case runs a real child process over real pipes: the deadlock, the
signal handling and the "no terminal event" path only exist there, and a
mocked stream would prove none of them.
"""
import sys
from pathlib import Path

import pytest

from . import pins
from .herder import (
    SYNTHETIC_IMAGE_ENV,
    Herder,
    HerderError,
    herder_argv,
    synthetic_image_for,
)

FAKE = str(Path(__file__).parent / "testdata" / "fake_herder.py")
PINNED = pins.EMU_VERSION
REQUEST = {"version": 1, "devices": [{"model": "USM8P"}]}


def fake(mode: str) -> list[str]:
    return [sys.executable, FAKE, mode]


@pytest.fixture
def herder():
    """Builds Herders and guarantees each is torn down, however it ended."""
    made = []

    def factory(mode: str, request: dict | None = None) -> Herder:
        h = Herder(fake(mode), request if request is not None else REQUEST)
        made.append(h)
        h.start()
        return h

    yield factory
    for h in made:
        h.kill()


# --- the argv builder -------------------------------------------------

def test_argv_reads_the_request_from_stdin():
    argv = herder_argv("/bin/herder", network="netA", inform_url="http://172.28.0.2:8080/inform")
    assert argv[0] == "/bin/herder"
    assert "--devices" in argv
    assert argv[argv.index("--devices") + 1] == "-"


def test_argv_carries_the_network_and_inform_url():
    argv = herder_argv("/bin/herder", network="netA", inform_url="http://172.28.0.2:8080/inform")
    assert argv[argv.index("--network") + 1] == "netA"
    assert argv[argv.index("--inform-url") + 1] == "http://172.28.0.2:8080/inform"


def test_argv_omits_synthetic_image_when_unset():
    # A release build has a version-matched default; passing an empty
    # override would break it, so the flag must be absent, not empty.
    argv = herder_argv("/bin/herder", network="n", inform_url="http://172.28.0.2:8080/inform")
    assert "--synthetic-image" not in argv


def test_argv_passes_a_selected_synthetic_image():
    argv = herder_argv("/bin/herder", network="n", inform_url="http://172.28.0.2:8080/inform",
                       synthetic_image="unifi-emu:dev")
    assert argv[argv.index("--synthetic-image") + 1] == "unifi-emu:dev"


# --- choosing the synthetic image for a given binary ------------------
#
# A release herder compiles in the image built from its own tag, so the
# right thing to pass is nothing at all. Only a development build, which
# has no default, needs one supplied.

def test_a_pinned_release_binary_needs_no_image_override():
    assert synthetic_image_for(PINNED, "") == ""


def test_an_explicit_override_wins_over_the_compiled_default():
    assert synthetic_image_for(PINNED, "unifi-emu:local") == "unifi-emu:local"


def test_a_development_build_requires_an_override():
    # No compiled default: without one the herder fails validate with
    # synthetic_image_required, so say so here instead. This goes through
    # the skip-vs-fail knob, and pytest outcomes derive from BaseException
    # rather than Exception — catching Exception would let the skip escape
    # and silently pass this test by not running it.
    with pytest.raises(BaseException, match=SYNTHETIC_IMAGE_ENV) as exc:
        synthetic_image_for("dev", "")
    assert exc.typename == "Skipped"


def test_a_development_build_hard_fails_where_a_skip_is_not_allowed(monkeypatch):
    # Same condition under UNIFI_TEST_REQUIRE, which CI always sets: no
    # skip may satisfy a required check.
    monkeypatch.setenv("UNIFI_TEST_REQUIRE", "1")
    with pytest.raises(BaseException, match=SYNTHETIC_IMAGE_ENV) as exc:
        synthetic_image_for("dev", "")
    assert exc.typename == "Failed"


def test_a_development_build_is_fine_with_an_override():
    assert synthetic_image_for("dev", "unifi-emu:local") == "unifi-emu:local"


def test_a_binary_off_the_pin_is_refused():
    # Drift: this suite would silently be testing a different emulator
    # than the one pins.py declares.
    with pytest.raises(HerderError, match=PINNED):
        synthetic_image_for("0.4.2", "")


def test_an_override_does_not_excuse_a_drifted_binary():
    # The image is only half of it — the protocol and the flags come from
    # the binary, so a stale one is drift no matter which image it starts.
    with pytest.raises(HerderError, match=PINNED):
        synthetic_image_for("0.4.2", "unifi-emu:local")


# --- the ready path ---------------------------------------------------

def test_ready_returns_the_resolved_identities(herder):
    devices = herder("ready").wait_ready(timeout_s=30)
    assert len(devices) == 1
    assert devices[0].model == "USM8P"
    assert devices[0].mac == "02:00:00:00:00:00"
    assert devices[0].ip == "172.28.0.4"


def test_ready_ignores_unknown_fields(herder):
    # The protocol may grow fields within version 1; an unknown one is not
    # an error and must not reach the caller as a surprise attribute.
    devices = herder("ready").wait_ready(timeout_s=30)
    assert not hasattr(devices[0], "runtime_hint")


def test_the_request_reaches_the_child_on_stdin(herder):
    request = {"version": 1, "devices": [{"model": "U7PRO"}, {"model": "UGW3"}]}
    devices = herder("ready", request).wait_ready(timeout_s=30)
    assert [d.model for d in devices] == ["U7PRO", "UGW3"]


def test_a_flooded_stderr_does_not_deadlock_ready(herder):
    # The child writes far more than a pipe buffer holds before `ready`. A
    # driver that reads stderr only at teardown never gets here.
    devices = herder("noisy").wait_ready(timeout_s=60)
    assert len(devices) == 1


def test_device_logs_are_kept_as_failure_evidence(herder):
    h = herder("noisy")
    h.wait_ready(timeout_s=60)
    assert "device log line 7999" in h.stderr_text


# --- teardown ---------------------------------------------------------

def test_stop_requires_the_terminal_stopped_event(herder):
    h = herder("ready")
    h.wait_ready(timeout_s=30)
    h.stop(timeout_s=30)
    assert h.returncode == 0


def test_stop_kills_a_herder_that_ignores_the_signal(herder):
    h = herder("deaf")
    h.wait_ready(timeout_s=30)
    with pytest.raises(HerderError, match="did not stop"):
        h.stop(timeout_s=1)
    assert h.returncode is not None  # killed, not left running


# --- failure paths ----------------------------------------------------

def test_a_failed_event_names_its_code_and_phase(herder):
    with pytest.raises(HerderError) as exc:
        herder("failed-early").wait_ready(timeout_s=30)
    assert "network_not_found" in str(exc.value)
    assert "validate" in str(exc.value)


def test_an_exit_without_a_terminal_event_surfaces_stderr(herder):
    # The 2026-08-01 herder bug: `started`, then a panic and exit 2 with no
    # terminal event. Silence here would read as a hung fixture.
    with pytest.raises(HerderError) as exc:
        herder("crash").wait_ready(timeout_s=30)
    message = str(exc.value)
    assert "exit 2" in message
    assert "panic" in message


def test_a_newer_protocol_version_is_ignored_not_interpreted(herder):
    # Fields may be added within protocol 1, so unknown fields are ignored.
    # A new version is a different contract this driver must not read as if
    # it were this one — but skipping it silently would surface as an
    # unexplained timeout, so the wait says what it skipped.
    with pytest.raises(HerderError, match="protocol 2"):
        herder("protocol2").wait_ready(timeout_s=1.0)


def test_non_protocol_output_on_stdout_is_still_an_error(herder):
    # stdout carries the protocol and nothing else, so a line that is not
    # even an event is a broken contract rather than a future one.
    with pytest.raises(HerderError, match="non-protocol"):
        herder("chatty").wait_ready(timeout_s=30)


def test_a_failure_after_ready_is_reported_at_stop(herder):
    h = herder("die-after-ready")
    h.wait_ready(timeout_s=30)
    with pytest.raises(HerderError):
        h.stop(timeout_s=10)


def test_a_binary_that_never_says_started_fails_on_the_short_clock(herder):
    # `started` precedes even request decoding, so its absence means this is
    # not the herder — the likeliest shape of a mistyped binary path. That
    # must not cost the whole startup budget in silence.
    h = herder("mute")
    with pytest.raises(HerderError, match="started"):
        h.wait_started(timeout_s=0.5)


def test_waiting_past_the_deadline_fails_loudly(herder):
    h = herder("silent")
    with pytest.raises(HerderError, match="ready"):
        h.wait_ready(timeout_s=0.5)
