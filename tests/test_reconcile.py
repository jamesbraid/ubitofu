# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 James Braid
"""Pipeline-level tests for `reconcile`: surgical, comment-preserving merge of
committed HCL toward live controller state.

The runner is stubbed (as in test_integration) so no real tofu/controller runs;
the fake plan carries resource_changes (change.before = live, change.after =
committed) plus planned_values for new objects.
"""
import io

import pytest

from ubitofu.config import Config
from ubitofu.enumerator import EnumerationResult, ImportTarget

# ---------------------------------------------------------------------------
# Schema + committed fixtures shared across cases.
# ---------------------------------------------------------------------------

SCHEMA = {"provider_schemas": {
    "registry.opentofu.org/ubiquiti-community/unifi": {"resource_schemas": {
        "unifi_network": {"block": {
            "attributes": {
                "name":    {"type": "string", "required": True},
                "vlan":    {"type": "number", "optional": True},
                "mtu":     {"type": "number", "optional": True},
                "enabled": {"type": "bool", "optional": True},
                "dhcp_server": {"optional": True, "nested_type": {
                    "nesting_mode": "single",
                    "attributes": {
                        "enabled": {"type": "bool", "optional": True},
                        "start":   {"type": "string", "optional": True},
                    }}},
            }}},
        "unifi_client": {"block": {"attributes": {
            "name":     {"type": "string", "required": True},
            "mac":      {"type": "string", "required": True},
            "fixed_ip": {"type": "string", "optional": True},
        }}},
    }}}}

COMMITTED_NETWORK_TF = '''# Networks — hand maintained, do not regenerate wholesale.
resource "unifi_network" "examplenet" {
  name    = "examplenet"
  vlan    = 10 # pinned VLAN, keep this comment
  mtu     = 1500
  enabled = true

  dhcp_server = {
    enabled = true
    start   = "10.0.0.10"
  }
}

# Legacy network — controller object was deleted out of band.
resource "unifi_network" "oldnet" {
  name = "oldnet"
  vlan = 66
}
'''


def _write_committed(workdir):
    (workdir / "networks.tf").write_text(COMMITTED_NETWORK_TF)


class FakeRunner:
    """Stub TofuRunner: returns a canned plan, schema and state."""

    def __init__(self, workdir, plan, state):
        self.workdir = workdir
        self._plan = plan
        self._state = state

    def plan(self, *, out=None, generate_config_out=None):
        if generate_config_out is not None:
            generate_config_out.write_text("# stub\n")
        return 0

    def providers_schema(self):
        return SCHEMA

    def show_json(self, plan_file):
        return self._plan

    def show_state_json(self):
        return self._state


def _run(monkeypatch, tmp_path, plan, targets, state):
    import ubitofu.pipeline as pl

    monkeypatch.setattr(pl, "Controller", lambda **kw: object())
    monkeypatch.setattr(pl, "enumerate_controller",
                        lambda ctl: EnumerationResult(targets=targets, gaps=[]))
    monkeypatch.setattr(pl, "TofuRunner",
                        lambda workdir: FakeRunner(workdir, plan, state))
    monkeypatch.setenv("UNIFI_API_KEY", "k")
    cfg = Config("https://unifi.example", "default", "env", "UNIFI_API_KEY",
                 "ExampleVault", workdir=str(tmp_path))
    out = io.StringIO()
    rc = pl.run_reconcile(cfg, "bulk", out)
    return rc, out.getvalue()


# state: examplenet (net001) and one client already managed.
STATE = {"values": {"root_module": {"resources": [
    {"type": "unifi_network", "name": "examplenet", "values": {"id": "net001"}},
]}}}


def _drift_plan():
    return {
        "resource_changes": [
            {"type": "unifi_network", "name": "examplenet",
             "change": {"actions": ["update"],
                        "before": {  # LIVE
                            "name": "examplenet", "vlan": 20, "mtu": 1500,
                            "enabled": True,
                            "dhcp_server": {"enabled": True, "start": "10.0.0.50"}},
                        "after": {   # COMMITTED
                            "name": "examplenet", "vlan": 10, "mtu": 1500,
                            "enabled": True,
                            "dhcp_server": {"enabled": True, "start": "10.0.0.10"}}}},
            {"type": "unifi_network", "name": "oldnet",
             "change": {"actions": ["create"],
                        "before": None,
                        "after": {"name": "oldnet", "vlan": 66}}},
        ],
        "planned_values": {"root_module": {"resources": [
            {"type": "unifi_client", "name": "laptop",
             "values": {"name": "laptop", "mac": "00:11:22:00:00:02",
                        "fixed_ip": "10.0.0.99"}},
        ]}},
    }


def _drift_targets():
    return [
        ImportTarget("unifi_network", "examplenet", "net001"),      # managed
        ImportTarget("unifi_client", "laptop", "00:11:22:00:00:02"),  # NEW
    ]


def test_reconcile_scalar_drift_edited_in_place(monkeypatch, tmp_path):
    _write_committed(tmp_path)
    rc, report = _run(monkeypatch, tmp_path, _drift_plan(), _drift_targets(), STATE)
    assert rc == 0
    text = (tmp_path / "networks.tf").read_text()
    # scalar drift merged: vlan 10 -> 20, comment + layout intact
    assert "vlan    = 20 # pinned VLAN, keep this comment" in text
    # every hand comment survives
    assert "# Networks — hand maintained, do not regenerate wholesale." in text
    assert "unifi_network.examplenet.vlan" in report
    assert "10" in report and "20" in report


def test_reconcile_flags_nested_drift_not_edited(monkeypatch, tmp_path):
    _write_committed(tmp_path)
    _, report = _run(monkeypatch, tmp_path, _drift_plan(), _drift_targets(), STATE)
    text = (tmp_path / "networks.tf").read_text()
    # dhcp_server.start drift is nested -> NOT auto-edited
    assert 'start   = "10.0.0.10"' in text
    assert "10.0.0.50" not in text
    # but it IS reported for manual review
    assert "dhcp_server" in report
    assert "manual review" in report.lower()


def test_reconcile_appends_new_object(monkeypatch, tmp_path):
    _write_committed(tmp_path)
    _, report = _run(monkeypatch, tmp_path, _drift_plan(), _drift_targets(), STATE)
    new_tf = (tmp_path / "reconciled_new.tf").read_text()
    assert 'resource "unifi_client" "laptop"' in new_tf
    assert '"00:11:22:00:00:02"' in new_tf or "00:11:22:00:00:02" in new_tf
    imports = (tmp_path / "imports.tf").read_text()
    assert "unifi_client.laptop" in imports
    assert '"00:11:22:00:00:02"' in imports
    assert "unifi_client.laptop" in report


def test_reconcile_flags_removed(monkeypatch, tmp_path):
    _write_committed(tmp_path)
    _, report = _run(monkeypatch, tmp_path, _drift_plan(), _drift_targets(), STATE)
    # oldnet is in committed config but the controller object is gone (create action)
    assert "oldnet" in report
    # committed block is left in place — deletions are operator's call
    assert 'resource "unifi_network" "oldnet"' in (tmp_path / "networks.tf").read_text()


def test_reconcile_secrets_never_written_plaintext(monkeypatch, tmp_path):
    # A sensitive attr drift must never be auto-written as plaintext.
    schema_key = "registry.opentofu.org/ubiquiti-community/unifi"
    rs = SCHEMA["provider_schemas"][schema_key]["resource_schemas"]
    rs["unifi_wlan"] = {"block": {"attributes": {
        "name":       {"type": "string", "required": True},
        "passphrase": {"type": "string", "optional": True, "sensitive": True},
    }}}
    (tmp_path / "wlan.tf").write_text(
        'resource "unifi_wlan" "wifi" {\n'
        '  name       = "wifi"\n'
        "  passphrase = var.wlan_wifi_psk\n"
        "}\n")
    plan = {
        "resource_changes": [
            {"type": "unifi_wlan", "name": "wifi",
             "change": {"actions": ["update"],
                        "before": {"name": "wifi", "passphrase": "live-secret-123"},
                        "after": {"name": "wifi", "passphrase": None}}}],
        "planned_values": {"root_module": {"resources": []}},
    }
    targets = [ImportTarget("unifi_wlan", "wifi", "wlan001")]
    state = {"values": {"root_module": {"resources": [
        {"type": "unifi_wlan", "name": "wifi", "values": {"id": "wlan001"}}]}}}
    _run(monkeypatch, tmp_path, plan, targets, state)
    text = (tmp_path / "wlan.tf").read_text()
    assert "live-secret-123" not in text        # NEVER plaintext
    assert "passphrase = var.wlan_wifi_psk" in text
    del rs["unifi_wlan"]


def test_reconcile_in_sync_is_noop(monkeypatch, tmp_path):
    _write_committed(tmp_path)
    before = (tmp_path / "networks.tf").read_text()
    plan = {
        "resource_changes": [
            {"type": "unifi_network", "name": "examplenet",
             "change": {"actions": ["no-op"], "before": {}, "after": {}}}],
        "planned_values": {"root_module": {"resources": []}},
    }
    targets = [ImportTarget("unifi_network", "examplenet", "net001")]
    rc, report = _run(monkeypatch, tmp_path, plan, targets, STATE)
    assert rc == 0
    assert (tmp_path / "networks.tf").read_text() == before   # byte-identical
    assert "sync" in report.lower()
    assert not (tmp_path / "reconciled_new.tf").exists()


def test_reconcile_append_is_idempotent(monkeypatch, tmp_path):
    _write_committed(tmp_path)
    # first run appends laptop
    _run(monkeypatch, tmp_path, _drift_plan(), _drift_targets(), STATE)
    first = (tmp_path / "reconciled_new.tf").read_text()
    # second run with the SAME "new" target must not duplicate the block
    _run(monkeypatch, tmp_path, _drift_plan(), _drift_targets(), STATE)
    second = (tmp_path / "reconciled_new.tf").read_text()
    assert first == second
    assert second.count('resource "unifi_client" "laptop"') == 1


def test_reconcile_new_same_name_device_gets_fresh_slug(monkeypatch, tmp_path):
    """Regression for slug-collision bug: a new device with the same base name as
    a managed device must get slug _2, never reusing the managed device's address.

    Trigger shape: :0b is enumerated FIRST (before the managed :0a) so that
    without the reserved-seeding fix, assign_slugs would hand it "u7_pro_wall" —
    the slug already declared in committed.tf for :0a.
    """
    committed_tf = (
        'resource "unifi_device" "u7_pro_wall" {\n'
        '  mac  = "58:d6:1f:00:00:0a"\n'
        '  name = "U7 Pro Wall"\n'
        '}\n'
    )
    (tmp_path / "committed.tf").write_text(committed_tf)

    device_schema = {"provider_schemas": {
        "registry.opentofu.org/ubiquiti-community/unifi": {"resource_schemas": {
            "unifi_device": {"block": {"attributes": {
                "mac":  {"type": "string", "required": True},
                "name": {"type": "string", "optional": True},
            }}}
        }}}}

    # State: MAC :0a is managed under slug u7_pro_wall.
    state = {"values": {"root_module": {"resources": [
        {"type": "unifi_device", "name": "u7_pro_wall",
         "values": {"mac": "58:d6:1f:00:00:0a"}},
    ]}}}

    # Plan: no resource_changes (both devices are new to this plan run).
    # planned_values carries the new device under the slug the fix produces.
    plan = {
        "resource_changes": [],
        "planned_values": {"root_module": {"resources": [
            {"type": "unifi_device", "name": "u7_pro_wall_2",
             "values": {"mac": "58:d6:1f:00:00:0b", "name": "U7 Pro Wall"}},
        ]}},
    }

    # :0b is FIRST — the ordering that triggers the bug in the unfixed code.
    targets = [
        ImportTarget("unifi_device", "U7 Pro Wall", "58:d6:1f:00:00:0b"),  # NEW — first
        ImportTarget("unifi_device", "U7 Pro Wall", "58:d6:1f:00:00:0a"),  # managed — second
    ]

    import ubitofu.pipeline as pl

    class DeviceRunner(FakeRunner):
        def providers_schema(self):
            return device_schema

    monkeypatch.setattr(pl, "Controller", lambda **kw: object())
    monkeypatch.setattr(pl, "enumerate_controller",
                        lambda ctl: EnumerationResult(targets=targets, gaps=[]))
    monkeypatch.setattr(pl, "TofuRunner",
                        lambda workdir: DeviceRunner(workdir, plan, state))
    monkeypatch.setenv("UNIFI_API_KEY", "k")
    cfg = Config("https://unifi.example", "default", "env", "UNIFI_API_KEY",
                 "ExampleVault", workdir=str(tmp_path))
    out = io.StringIO()
    rc = pl.run_reconcile(cfg, "bulk", out)

    assert rc == 0
    new_tf = (tmp_path / "reconciled_new.tf").read_text()
    assert 'resource "unifi_device" "u7_pro_wall_2"' in new_tf
    assert "58:d6:1f:00:00:0b" in new_tf
    # Never re-emit the managed device's address
    assert 'resource "unifi_device" "u7_pro_wall"' not in new_tf


def test_reconcile_edit_survives_tofu_fmt(monkeypatch, tmp_path):
    import shutil
    import subprocess
    if shutil.which("tofu") is None:
        pytest.skip("tofu not on PATH")
    _write_committed(tmp_path)
    _run(monkeypatch, tmp_path, _drift_plan(), _drift_targets(), STATE)
    # the edited committed file must still be valid, fmt-stable HCL
    text = (tmp_path / "networks.tf").read_text()
    proc = subprocess.run(["tofu", "fmt", "-"], input=text,
                          capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == text        # already canonically formatted
