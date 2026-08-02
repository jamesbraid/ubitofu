# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 James Braid
"""Unit tests for support.py helpers that don't need docker (unmarked)."""
import pytest

from .readiness import ReadinessError
from .support import (
    SEEDED,
    SIM,
    _endpoint_ipv4,
    _external_device_host,
    _report_keep,
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
