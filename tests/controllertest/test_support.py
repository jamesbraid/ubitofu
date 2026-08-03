# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 James Braid
"""Unit tests for support.py helpers that don't need docker (unmarked)."""
import pytest

from .readiness import ReadinessError
from .support import (
    SEEDED,
    SIM,
    UOS_RUN_KWARGS,
    _endpoint_ipv4,
    _external_device_host,
    _release,
    _report_keep,
    _split_tmpfs,
    check_inform_url,
    inform_url,
)


def test_report_keep_warns_instead_of_printing(capsys):
    # UNIFI_TEST_KEEP's notice used to be a bare print(), which pytest
    # capture swallows unless a run passes -s — the operator would leave
    # a container running with no visible confirmation. warnings.warn
    # lands in the warnings summary unconditionally.
    with pytest.warns(UserWarning, match="UNIFI_TEST_KEEP"):
        _report_keep(SEEDED, "https://127.0.0.1:12345")
    # Nothing goes to stdout/stderr any more — the warning is the only signal.
    captured = capsys.readouterr()
    assert captured.out == ""


def test_report_keep_names_flavor_and_base_url():
    with pytest.warns(UserWarning) as record:
        _report_keep(SEEDED, "https://127.0.0.1:12345")
    assert len(record) == 1
    message = str(record[0].message)
    assert "seeded" in message
    assert "https://127.0.0.1:12345" in message


# --- the inform URL the herder will accept ----------------------------
#
# Its form is exactly http://<canonical-IPv4-literal>:<port>/inform. The
# narrowness is the contract: device containers resolve nothing, so anything
# else produces a fleet that starts cleanly and never adopts. Rejecting it
# here names the problem in this fixture instead of in a child process.

def test_inform_url_is_the_canonical_ipv4_form():
    assert inform_url("172.28.0.2", 8080) == "http://172.28.0.2:8080/inform"


def test_inform_url_rejects_a_hostname():
    with pytest.raises(ValueError, match="IPv4"):
        inform_url("controller", 8080)


def test_inform_url_rejects_a_loopback_address():
    # Reachable from the harness, unreachable from a device container.
    with pytest.raises(ValueError, match="reachable"):
        inform_url("127.0.0.1", 8080)


def test_inform_url_rejects_a_non_canonical_literal():
    # A leading-zero octet reaches the controller as a different string
    # than the one it compares against.
    with pytest.raises(ValueError, match="IPv4"):
        inform_url("172.028.0.2", 8080)


def test_inform_url_rejects_an_ipv6_address():
    with pytest.raises(ValueError, match="IPv4"):
        inform_url("fd00::2", 8080)


def test_check_inform_url_accepts_the_canonical_form():
    assert check_inform_url("http://172.28.0.2:8080/inform") == "http://172.28.0.2:8080/inform"


@pytest.mark.parametrize("bad", [
    "https://172.28.0.2:8080/inform",       # devices speak http
    "http://172.28.0.2:8080/",              # wrong path
    "http://172.28.0.2:8080/inform?x=1",    # query
    "http://172.28.0.2/inform",             # no explicit port
    "http://controller:8080/inform",        # hostname
    "http://172.28.0.2:8080/inform/",       # trailing slash
])
def test_check_inform_url_rejects_off_contract_urls(bad):
    with pytest.raises(ValueError):
        check_inform_url(bad)


# --- reading the controller's address on our own network ---------------

def _attrs(networks: dict) -> dict:
    return {"NetworkSettings": {"Networks": networks}}


def test_endpoint_ipv4_reads_the_named_network():
    # Attaching at create time leaves the controller single-homed today
    # (verified: one interface, and it advertises that same address back
    # post-adopt). Picking by name anyway means a second attachment can
    # never turn the inform host into a lottery.
    attrs = _attrs({
        "bridge": {"IPAddress": "172.17.0.5"},
        "herder-net": {"IPAddress": "172.28.0.2"},
    })
    assert _endpoint_ipv4(attrs, "herder-net") == "172.28.0.2"


def test_endpoint_ipv4_fails_when_the_controller_is_not_on_the_network():
    with pytest.raises(ValueError, match="herder-net"):
        _endpoint_ipv4(_attrs({"bridge": {"IPAddress": "172.17.0.5"}}), "herder-net")


def test_endpoint_ipv4_fails_on_an_empty_address():
    # An attached-but-addressless endpoint means the inspection was read
    # too early; an empty host would build a nonsense URL.
    with pytest.raises(ValueError, match="no IPv4"):
        _endpoint_ipv4(_attrs({"herder-net": {"IPAddress": ""}}), "herder-net")


# --- what an external controller may offer a device fleet --------------

def test_an_external_controller_offers_no_device_host_by_default(monkeypatch):
    monkeypatch.delenv(SIM.network_env, raising=False)
    monkeypatch.delenv(SIM.inform_env, raising=False)
    assert _external_device_host(SIM) == ("", "")


def test_an_external_controller_can_supply_both_halves(monkeypatch):
    monkeypatch.setenv(SIM.network_env, "ci-net")
    monkeypatch.setenv(SIM.inform_env, "http://10.1.2.3:8080/inform")
    assert _external_device_host(SIM) == ("ci-net", "http://10.1.2.3:8080/inform")


def test_half_a_pair_is_a_configuration_error(monkeypatch):
    # A network with no inform URL starts devices that never adopt; an
    # inform URL with no network has nothing to start them on. Neither is
    # a usable fallback, so neither may quietly become one.
    monkeypatch.setenv(SIM.network_env, "ci-net")
    monkeypatch.delenv(SIM.inform_env, raising=False)
    with pytest.raises(ReadinessError, match=SIM.inform_env):
        _external_device_host(SIM)


def test_an_off_contract_inform_url_names_its_variable(monkeypatch):
    monkeypatch.setenv(SIM.network_env, "ci-net")
    monkeypatch.setenv(SIM.inform_env, "http://controller:8080/inform")
    with pytest.raises(ReadinessError, match=SIM.inform_env):
        _external_device_host(SIM)


# --- tmpfs must not ride in on **kwargs -------------------------------
#
# DockerContainer takes no `tmpfs` parameter: it initialises self.tmpfs to {}
# and sweeps anything it does not name into **kwargs. start() then calls
# create(tmpfs=self.tmpfs, **kwargs), so a tmpfs passed as a run kwarg
# arrives twice and the container never starts:
#
#   TypeError: DockerClient.create() got multiple values for keyword 'tmpfs'
#
# It has to go through the API that owns that attribute instead.

def test_tmpfs_is_split_out_of_run_kwargs():
    kwargs, tmpfs = _split_tmpfs({"cgroupns": "host", "tmpfs": {"/run": "exec"}})
    assert "tmpfs" not in kwargs
    assert tmpfs == {"/run": "exec"}


def test_splitting_tmpfs_leaves_every_other_run_kwarg_alone():
    kwargs, _ = _split_tmpfs(dict(UOS_RUN_KWARGS))
    assert kwargs["cgroupns"] == "host"
    assert kwargs["cap_drop"] == ["ALL"]
    assert "SYS_ADMIN" in kwargs["cap_add"]


def test_the_uos_contract_mounts_survive_the_split():
    # The documented UOS runtime contract: systemd as PID 1 needs these, and
    # their option strings are not sizes — they must arrive verbatim.
    _, tmpfs = _split_tmpfs(dict(UOS_RUN_KWARGS))
    assert tmpfs == {
        "/run": "exec", "/run/lock": "", "/tmp": "exec",
        "/var/lib/journal": "", "/var/opt/unifi/tmp": "size=64m",
    }


def test_a_flavor_with_no_tmpfs_is_unchanged():
    kwargs, tmpfs = _split_tmpfs({"cgroupns": "host"})
    assert kwargs == {"cgroupns": "host"}
    assert tmpfs == {}


def test_splitting_does_not_mutate_the_shared_contract():
    # UOS_RUN_KWARGS is a module-level dict every UOS boot reuses; popping
    # from it in place would leave the second boot of a session with no
    # tmpfs at all.
    _split_tmpfs(UOS_RUN_KWARGS)
    assert "tmpfs" in UOS_RUN_KWARGS


# --- releasing the container and the network it sits on ----------------
#
# The network is created per boot, and Docker's default pool holds only a
# handful of them. A teardown path that skips removal leaks one per run
# until boots start failing on address-pool exhaustion, so removal has to
# be attempted whatever the container does on the way out.


class _Stub:
    """A container or network that records calls and can be made to fail."""

    def __init__(self, name="net-1", fail=None):
        self.name = name
        self.calls: list[str] = []
        self._fail = fail

    def _record(self, what):
        self.calls.append(what)
        if self._fail == what:
            raise RuntimeError(f"{what} exploded")

    def stop(self):
        self._record("stop")

    def remove(self):
        self._record("remove")


def test_release_stops_the_container_before_removing_the_network():
    # A network still holding a container cannot be removed, so the order
    # is load-bearing rather than incidental.
    container, network = _Stub(), _Stub()
    _release(container, network)
    assert container.calls == ["stop"]
    assert network.calls == ["remove"]


def test_the_network_is_removed_even_when_the_container_will_not_stop():
    container, network = _Stub(fail="stop"), _Stub()
    with pytest.raises(RuntimeError, match="stop exploded"):
        _release(container, network)
    assert network.calls == ["remove"], "the network leaked when stop() raised"


def test_a_failed_removal_is_reported_rather_than_swallowed():
    # Losing a network silently is how the pool fills up unnoticed.
    container, network = _Stub(), _Stub(name="net-9", fail="remove")
    with pytest.warns(UserWarning, match="net-9"):
        _release(container, network)


def test_a_failed_removal_does_not_mask_why_the_container_failed():
    # Removal fails *because* the container is still attached, so its error
    # is a consequence. The original cause has to survive.
    container, network = _Stub(fail="stop"), _Stub(fail="remove")
    with pytest.warns(UserWarning), pytest.raises(RuntimeError, match="stop exploded"):
        _release(container, network)
