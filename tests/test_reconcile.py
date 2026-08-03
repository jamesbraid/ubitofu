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

# run_reconcile now also runs the coverage audit (_emit_coverage), which reads
# unifi_setting out of the schema and refuses to run blind if it is absent —
# every fake schema below carries this minimal stub so the audit no-ops
# cleanly instead of raising KeyError.
_SETTING_STUB = {"unifi_setting": {"block": {"attributes": {
    "site": {"type": "string", "optional": True},
}}}}


class FakeCoverageController:
    """Minimal Controller stand-in: no config beyond site, empty everywhere."""

    site = "default"

    def __init__(self):
        self.closed = False

    def collection(self, endpoint):
        return []

    def close(self):
        self.closed = True


SCHEMA = {"provider_schemas": {
    "registry.opentofu.org/ubiquiti-community/unifi": {"resource_schemas": {
        "unifi_network": {"block": {
            "attributes": {
                "name":    {"type": "string", "required": True},
                "vlan":    {"type": "number", "optional": True},
                "mtu":     {"type": "number", "optional": True},
                "enabled": {"type": "bool", "optional": True},
                # Settable, and deliberately absent from COMMITTED_NETWORK_TF:
                # the stand-in for an attribute only the provider has an
                # opinion about (see the provider-default section below).
                "mdns":    {"type": "bool", "optional": True},
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
        **_SETTING_STUB,
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

    monkeypatch.setattr(pl, "controller_from_config", lambda cfg: FakeCoverageController())
    monkeypatch.setattr(pl, "enumerate_controller",
                        lambda ctl: EnumerationResult(targets=targets, gaps=[]))
    monkeypatch.setattr(pl, "TofuRunner",
                        lambda workdir: FakeRunner(workdir, plan, state))
    monkeypatch.setenv("UNIFI_API_KEY", "k")
    cfg = Config("https://unifi.example", "default", "env", "UNIFI_API_KEY",
                 "ExampleVault", workdir=str(tmp_path))
    out = io.StringIO()
    rc = pl.run_reconcile(cfg, out)
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
            {"type": "unifi_client", "name": "client_a",
             "values": {"name": "client_a", "mac": "00:11:22:00:00:02",
                        "fixed_ip": "10.0.0.99"}},
        ]}},
    }


def _drift_targets():
    return [
        ImportTarget("unifi_network", "examplenet", "net001"),      # managed
        ImportTarget("unifi_client", "client_a", "00:11:22:00:00:02"),  # NEW
    ]


def test_reconcile_scalar_drift_edited_in_place(monkeypatch, tmp_path):
    _write_committed(tmp_path)
    rc, report = _run(monkeypatch, tmp_path, _drift_plan(), _drift_targets(), STATE)
    assert rc == 12      # drift captured AND attention flagged (nested dhcp_server drift)
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
    assert 'resource "unifi_client" "client_a"' in new_tf
    assert '"00:11:22:00:00:02"' in new_tf or "00:11:22:00:00:02" in new_tf
    # import block is in reconciled_new.tf alongside the resource block
    assert "unifi_client.client_a" in new_tf
    assert "import {" in new_tf
    # reconcile never creates or touches the operator's imports.tf
    assert not (tmp_path / "imports.tf").exists()
    assert "unifi_client.client_a" in report


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


def test_reconcile_closes_the_controller(monkeypatch, tmp_path):
    # Item 2: run_reconcile must close the controller it builds, same as
    # run_generate — never leak the http client past the run.
    import ubitofu.pipeline as pl

    _write_committed(tmp_path)
    plan = {
        "resource_changes": [
            {"type": "unifi_network", "name": "examplenet",
             "change": {"actions": ["no-op"], "before": {}, "after": {}}}],
        "planned_values": {"root_module": {"resources": []}},
    }
    targets = [ImportTarget("unifi_network", "examplenet", "net001")]
    sentinel_ctl = FakeCoverageController()
    monkeypatch.setattr(pl, "controller_from_config", lambda cfg: sentinel_ctl)
    monkeypatch.setattr(pl, "enumerate_controller",
                        lambda ctl: EnumerationResult(targets=targets, gaps=[]))
    monkeypatch.setattr(pl, "TofuRunner",
                        lambda workdir: FakeRunner(workdir, plan, STATE))
    monkeypatch.setenv("UNIFI_API_KEY", "k")
    cfg = Config("https://unifi.example", "default", "env", "UNIFI_API_KEY",
                 "ExampleVault", workdir=str(tmp_path))
    pl.run_reconcile(cfg, io.StringIO())

    assert sentinel_ctl.closed is True


def test_reconcile_append_is_idempotent(monkeypatch, tmp_path):
    _write_committed(tmp_path)
    # first run appends client_a
    _run(monkeypatch, tmp_path, _drift_plan(), _drift_targets(), STATE)
    first = (tmp_path / "reconciled_new.tf").read_text()
    # second run with the SAME "new" target must not duplicate the block
    _run(monkeypatch, tmp_path, _drift_plan(), _drift_targets(), STATE)
    second = (tmp_path / "reconciled_new.tf").read_text()
    assert first == second
    assert second.count('resource "unifi_client" "client_a"') == 1


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
            }}},
            **_SETTING_STUB,
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

    monkeypatch.setattr(pl, "controller_from_config", lambda cfg: FakeCoverageController())
    monkeypatch.setattr(pl, "enumerate_controller",
                        lambda ctl: EnumerationResult(targets=targets, gaps=[]))
    monkeypatch.setattr(pl, "TofuRunner",
                        lambda workdir: DeviceRunner(workdir, plan, state))
    monkeypatch.setenv("UNIFI_API_KEY", "k")
    cfg = Config("https://unifi.example", "default", "env", "UNIFI_API_KEY",
                 "ExampleVault", workdir=str(tmp_path))
    out = io.StringIO()
    rc = pl.run_reconcile(cfg, out)

    assert rc == 10      # append captured, nothing flagged
    new_tf = (tmp_path / "reconciled_new.tf").read_text()
    assert 'resource "unifi_device" "u7_pro_wall_2"' in new_tf
    assert "58:d6:1f:00:00:0b" in new_tf
    # Never re-emit the managed device's address
    assert 'resource "unifi_device" "u7_pro_wall"' not in new_tf


def test_reconcile_does_not_touch_operator_imports_tf(monkeypatch, tmp_path):
    """An operator-committed imports.tf must survive byte-identical; reconcile must
    never write to it or delete it."""
    _write_committed(tmp_path)
    imports = tmp_path / "imports.tf"
    imports.write_text('# operator-owned\nimport {\n  to = unifi_network.x\n  id = "abc"\n}\n')
    before = imports.read_text()
    _run(monkeypatch, tmp_path, _drift_plan(), _drift_targets(), STATE)
    assert imports.read_text() == before  # byte-identical, never clobbered


def test_reconcile_prelude_scratch_is_cleaned_up(monkeypatch, tmp_path):
    """The unique tempfile scratch must be removed after a successful run."""
    _write_committed(tmp_path)
    _run(monkeypatch, tmp_path, _drift_plan(), _drift_targets(), STATE)
    leftovers = list(tmp_path.glob("ubitofu-reconcile-*.tf"))
    assert leftovers == []  # try/finally removed the scratch


def test_reconcile_scratch_cleaned_even_on_tofu_failure(monkeypatch, tmp_path):
    """Scratch must be removed even when runner.plan raises (try/finally guard)."""
    import ubitofu.pipeline as pl

    class RaisingRunner:
        def __init__(self, workdir):
            self.workdir = workdir

        def show_state_json(self):
            return {"values": {"root_module": {"resources": []}}}

        def plan(self, *, out=None, generate_config_out=None):
            raise RuntimeError("tofu blew up")

        def providers_schema(self):
            return SCHEMA

        def show_json(self, plan_file):
            return {}

    monkeypatch.setattr(pl, "controller_from_config", lambda cfg: FakeCoverageController())
    monkeypatch.setattr(pl, "enumerate_controller",
                        lambda ctl: EnumerationResult(targets=_drift_targets(), gaps=[]))
    monkeypatch.setattr(pl, "TofuRunner", lambda workdir: RaisingRunner(workdir))
    monkeypatch.setenv("UNIFI_API_KEY", "k")
    cfg = Config("https://unifi.example", "default", "env", "UNIFI_API_KEY",
                 "ExampleVault", workdir=str(tmp_path))
    _write_committed(tmp_path)
    out = io.StringIO()
    with pytest.raises(RuntimeError, match="tofu blew up"):
        pl.run_reconcile(cfg, out)
    assert list(tmp_path.glob("ubitofu-reconcile-*.tf")) == []


def test_reconcile_scratch_cleaned_on_prelude_write_failure(monkeypatch, tmp_path):
    """Scratch must be removed even when write_text raises before runner.plan.

    The try/finally must cover the write window, not just the plan call.
    """
    import ubitofu.pipeline as pl

    def _raising_import_block(*a, **kw):
        raise RuntimeError("write blew up")

    monkeypatch.setattr(pl, "controller_from_config", lambda cfg: FakeCoverageController())
    monkeypatch.setattr(pl, "enumerate_controller",
                        lambda ctl: EnumerationResult(targets=_drift_targets(), gaps=[]))
    monkeypatch.setattr(pl, "TofuRunner",
                        lambda workdir: FakeRunner(workdir, _drift_plan(), STATE))
    monkeypatch.setattr(pl, "_import_block", _raising_import_block)
    monkeypatch.setenv("UNIFI_API_KEY", "k")
    cfg = Config("https://unifi.example", "default", "env", "UNIFI_API_KEY",
                 "ExampleVault", workdir=str(tmp_path))
    _write_committed(tmp_path)
    out = io.StringIO()
    with pytest.raises(RuntimeError, match="write blew up"):
        pl.run_reconcile(cfg, out)
    assert list(tmp_path.glob("ubitofu-reconcile-*.tf")) == []


def test_reconcile_scratch_cleaned_when_later_temp_allocation_fails(
    monkeypatch, tmp_path
):
    import ubitofu.pipeline as pl

    real_mkstemp = pl.tempfile.mkstemp
    calls = 0

    def failing_mkstemp(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("temp allocation failed")
        return real_mkstemp(*args, **kwargs)

    monkeypatch.setattr(pl, "controller_from_config", lambda cfg: FakeCoverageController())
    monkeypatch.setattr(pl, "enumerate_controller",
                        lambda ctl: EnumerationResult(targets=_drift_targets(), gaps=[]))
    monkeypatch.setattr(pl, "TofuRunner",
                        lambda workdir: FakeRunner(workdir, _drift_plan(), STATE))
    monkeypatch.setattr(pl.tempfile, "mkstemp", failing_mkstemp)
    monkeypatch.setenv("UNIFI_API_KEY", "k")
    cfg = Config("https://unifi.example", "default", "env", "UNIFI_API_KEY",
                 "ExampleVault", workdir=str(tmp_path))
    _write_committed(tmp_path)

    with pytest.raises(OSError, match="temp allocation failed"):
        pl.run_reconcile(cfg, io.StringIO())

    assert list(tmp_path.glob("ubitofu-reconcile-*.tf")) == []


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


def test_reconcile_reports_missing_config_delete_as_pending(monkeypatch, tmp_path, fixtures_dir):
    """An exact missing-config delete is apply intent, not reconcile drift."""
    import json
    plan = json.loads((fixtures_dir / "reconcile" / "plan_orphan.json").read_text())
    plan["resource_changes"][1]["action_reason"] = "delete_because_no_resource_config"
    plan.setdefault("planned_values", {"root_module": {"resources": []}})
    _write_committed(tmp_path)
    targets = [ImportTarget("unifi_network", "examplenet", "net001")]
    state = {"values": {"root_module": {"resources": [
        *STATE["values"]["root_module"]["resources"],
        {"type": "unifi_port_forward", "name": "example_fwd",
         "values": {"id": "00112233445566778899aabb"}},
    ]}}}
    rc, report = _run(monkeypatch, tmp_path, plan, targets, state)
    assert rc == 0
    assert "example_fwd" in report
    assert "— destroy" in report


def test_reconcile_new_secret_object_emits_variable_decl_and_warning(monkeypatch, tmp_path):
    """A new WLAN (secret-bearing) must emit a variable decl and actionable warning."""
    import ubitofu.pipeline as pl

    wlan_schema = {"provider_schemas": {
        "registry.opentofu.org/ubiquiti-community/unifi": {"resource_schemas": {
            "unifi_wlan": {"block": {"attributes": {
                "name":       {"type": "string", "required": True},
                "passphrase": {"type": "string", "optional": True, "sensitive": True},
                "security":   {"type": "string", "optional": True},
            }}},
            **_SETTING_STUB,
        }}}}

    plan = {
        "resource_changes": [],
        "planned_values": {"root_module": {"resources": [
            {"type": "unifi_wlan", "name": "example_net",
             "values": {"name": "example-wifi", "passphrase": "REDACTED",
                        "security": "wpapsk"}},
        ]}},
    }
    targets = [ImportTarget("unifi_wlan", "example_net", "wlan001")]
    state = {"values": {"root_module": {"resources": []}}}

    class WlanRunner(FakeRunner):
        def providers_schema(self):
            return wlan_schema

    monkeypatch.setattr(pl, "controller_from_config", lambda cfg: FakeCoverageController())
    monkeypatch.setattr(pl, "enumerate_controller",
                        lambda ctl: EnumerationResult(targets=targets, gaps=[]))
    monkeypatch.setattr(pl, "TofuRunner",
                        lambda workdir: WlanRunner(workdir, plan, state))
    monkeypatch.setenv("UNIFI_API_KEY", "k")
    cfg = Config("https://unifi.example", "default", "env", "UNIFI_API_KEY",
                 "ExampleVault", workdir=str(tmp_path))
    out = io.StringIO()
    rc = pl.run_reconcile(cfg, out)
    report = out.getvalue()

    assert rc == 12      # append captured AND secret var to declare
    variables_tf = (tmp_path / "unifi-variables.tf").read_text()
    assert "sensitive = true" in variables_tf
    new_tf = (tmp_path / "reconciled_new.tf").read_text()
    assert "REDACTED" not in new_tf   # no plaintext secret ever written
    assert "var.wlan_" in new_tf
    assert "TF_VAR_wlan_example_net_psk" in report


# ---------------------------------------------------------------------------
# Task 10: precise complex-drift flags via deepdiff
# ---------------------------------------------------------------------------

def test_complex_drift_flag_names_the_nested_path():
    """reconcile_complex_flags must produce a path+old→new flag for nested drift.

    before (live) has port_override[0].forward = 'native';
    after (committed) has 'customize'. The flag must name the sub-path and
    include both values — not a bare attr name or a generic 'manual review'.
    """
    from ubitofu.pipeline import reconcile_complex_flags

    before = {"port_override": [{"forward": "native", "speed": 1000}]}   # live
    after  = {"port_override": [{"forward": "customize", "speed": 1000}]} # committed
    flags = reconcile_complex_flags(before, after, "unifi_device.x")
    assert any(
        "port_override" in f and "native" in f and "customize" in f
        for f in flags
    ), f"No matching flag found; got: {flags}"


def test_complex_drift_flag_scalar_attrs_skipped():
    """Scalar attr diffs must not appear in reconcile_complex_flags output.

    Scalars go through update_scalar; the helper must not double-flag them.
    """
    from ubitofu.pipeline import reconcile_complex_flags

    before = {"name": "old-name", "vlan": 10}
    after  = {"name": "old-name", "vlan": 20}
    flags = reconcile_complex_flags(before, after, "unifi_network.lan")
    assert flags == [], f"Expected no flags for scalar-only diff; got: {flags}"


def test_complex_drift_flag_absent_attr_reported():
    """An attr present in committed but absent on controller gets a flag."""
    from ubitofu.pipeline import reconcile_complex_flags

    before = {}                        # live: attr missing
    after  = {"dhcp_server": {"enabled": True}}  # committed: has it
    flags = reconcile_complex_flags(before, after, "unifi_network.lan")
    assert any("dhcp_server" in f and "absent" in f for f in flags), \
        f"Expected absent-on-controller flag; got: {flags}"


# ---------------------------------------------------------------------------
# Plan-unknown values are pending an apply, not drift.
#
# A committed value that references a resource this same apply creates is
# unknown at plan time: tofu emits null in `after` and records the path in
# `after_unknown`, and cleaner.is_empty then drops the null. The attribute
# therefore looks absent-from-config and used to be flagged as uncaptured
# complex drift, which deadlocked the apply gate at exit 11 — reconcile
# cannot capture it, so its advice to "run reconcile" was unsatisfiable.
# ---------------------------------------------------------------------------

def test_unknown_nested_value_is_not_complex_drift():
    """A nested leaf marked unknown in the plan must not be flagged as drift."""
    from ubitofu.pipeline import reconcile_complex_flags

    live = {"port_override": [{"port_idx": 25, "port_profile_id": "OLDPROFILE"}]}
    committed = {"port_override": [{"port_idx": 25}]}   # id nulled -> dropped
    unknown = {"port_override": [{"port_profile_id": True}]}

    assert reconcile_complex_flags(live, committed, "unifi_device.x") != [], \
        "precondition: without the plan's unknown map this looks like drift"
    assert reconcile_complex_flags(live, committed, "unifi_device.x",
                                   unknown=unknown) == []


def test_unknown_whole_attr_is_not_complex_drift():
    """A whole attribute unknown in the plan is pending, not absent-from-config."""
    from ubitofu.pipeline import reconcile_complex_flags

    live = {"dhcp_server": {"enabled": True}}
    committed: dict = {}                       # nulled by the unknown, then dropped
    unknown = {"dhcp_server": True}

    assert reconcile_complex_flags(live, committed, "unifi_network.lan",
                                   unknown=unknown) == []


def test_known_drift_still_flagged_alongside_an_unknown_sibling():
    """Suppression is per-path: real drift beside an unknown must still flag.

    Guards the obvious over-correction — treating the whole resource as pending
    because one attribute happens to be unknown.
    """
    from ubitofu.pipeline import reconcile_complex_flags

    live = {"port_override": [{"port_profile_id": "OLDPROFILE", "forward": "native"}]}
    committed = {"port_override": [{"forward": "customize"}]}
    unknown = {"port_override": [{"port_profile_id": True}]}

    flags = reconcile_complex_flags(live, committed, "unifi_device.x",
                                    unknown=unknown)
    assert any("forward" in f for f in flags), f"real drift lost; got: {flags}"
    assert not any("port_profile_id" in f for f in flags), \
        f"unknown path still flagged; got: {flags}"


def test_diff_resource_threads_after_unknown_from_the_plan(tmp_path):
    """_diff_resource must pass the plan's unknown map down to the flagger.

    End-to-end for the gate deadlock: a device whose port_override references a
    port profile this apply creates produced a complex-drift flag, which set
    `flagged`, which returned EXIT_ATTENTION, which the gate blocks on — and
    reconcile could never capture it, so the block was permanent.
    """
    from ubitofu import pipeline as pl

    path = tmp_path / "device.tf"
    path.write_text('resource "unifi_device" "sw" {\n  mac = "aa:bb:cc:dd:ee:ff"\n}\n')
    live = {"port_override": [{"port_idx": 25, "port_profile_id": "OLDPROFILE"}]}
    committed = {"port_override": [{"port_idx": 25}]}
    unknown = {"port_override": [{"port_profile_id": True}]}

    flags: list[str] = []
    pl._diff_resource("unifi_device", "sw", live, committed, path, [], flags,
                      check=True, unknown=unknown)
    assert flags == [], f"pending reference flagged as drift; got: {flags}"

    without: list[str] = []
    pl._diff_resource("unifi_device", "sw", live, committed, path, [], without,
                      check=True)
    assert without, "precondition: unthreaded, this is still flagged as drift"


def test_unknown_map_absent_preserves_existing_behaviour():
    """No unknown map (None) must behave exactly as before."""
    from ubitofu.pipeline import reconcile_complex_flags

    live = {"port_override": [{"forward": "native"}]}
    committed = {"port_override": [{"forward": "customize"}]}
    assert reconcile_complex_flags(live, committed, "unifi_device.x") == \
        reconcile_complex_flags(live, committed, "unifi_device.x", unknown=None)


def test_complex_drift_deepdiff_exception_degrades_gracefully(monkeypatch):
    """When DeepDiff raises, reconcile_complex_flags must return the generic
    flag for that resource attr and must NOT propagate the exception.

    One bad resource must never abort the whole reconcile run.
    """
    import ubitofu.pipeline as pl

    # Patch DeepDiff to raise unconditionally
    monkeypatch.setattr(pl, "DeepDiff", _raising_deepdiff)

    live      = {"port_override": [{"forward": "native"}]}
    committed = {"port_override": [{"forward": "customize"}]}
    # Must not raise; must return the generic degraded flag
    flags = pl.reconcile_complex_flags(live, committed, "unifi_device.x")
    assert len(flags) == 1
    assert "unifi_device.x.port_override" in flags[0]
    assert "manual review" in flags[0]


def _raising_deepdiff(*args, **kwargs):
    raise TypeError("unhashable type: 'list'")


# ---------------------------------------------------------------------------
# Idempotence at the persisted-file level: reconciled_new.tf must never grow
# across re-runs even when slug assignment shifts due to the prior output
# entering the reserved set.
# ---------------------------------------------------------------------------

def test_reconcile_persisted_new_idempotent_across_slug_shift(monkeypatch, tmp_path):
    """reconciled_new.tf must be BYTE-IDENTICAL on run 2 even when the slug shifts.

    Root cause: reconciled_new.tf enters _committed_tf_files → its slug enters
    reserved → assign_slugs promotes the same object to 'client_a_2' → real tofu
    returns 'client_a_2' in planned_values → the append loop finds the entry and
    re-appends. Run 3 → 'client_a_3'. The fix matches by import_id (stable id),
    not slug, so the already-emitted object is filtered before slug lookup.

    RED (before fix): after_run2 != after_run1 — 'client_a_2' appended.
    GREEN (after fix): after_run2 == after_run1 — file untouched.
    """
    _write_committed(tmp_path)

    # Run 1: vanilla static plan → client_a appended as 'client_a'.
    _run(monkeypatch, tmp_path, _drift_plan(), _drift_targets(), STATE)
    after_run1 = (tmp_path / "reconciled_new.tf").read_text()
    assert 'resource "unifi_client" "client_a"' in after_run1

    # Run 2: simulate what real tofu produces when 'client_a' is now reserved.
    # planned_values uses 'client_a_2' as the slug (tofu sees client_a in reserved);
    # resource_changes are unchanged. This is the shape a real tofu plan emits
    # on the second reconcile call.
    plan_run2 = {
        "resource_changes": _drift_plan()["resource_changes"],
        "planned_values": {"root_module": {"resources": [
            {"type": "unifi_client", "name": "client_a_2",
             "values": {"name": "client_a", "mac": "00:11:22:00:00:02",
                        "fixed_ip": "10.0.0.99"}},
        ]}},
    }
    _run(monkeypatch, tmp_path, plan_run2, _drift_targets(), STATE)
    after_run2 = (tmp_path / "reconciled_new.tf").read_text()

    assert after_run2 == after_run1, (
        "reconciled_new.tf must be BYTE-IDENTICAL after run 2 — "
        f"'client_a_2' must not be appended.\nGot:\n{after_run2!r}"
    )
    assert "client_a_2" not in after_run2


# ---------------------------------------------------------------------------
# WireGuard peer identity bug: composite vs bare import_id mismatch
# ---------------------------------------------------------------------------

_WG_SCHEMA = {"provider_schemas": {
    "registry.opentofu.org/ubiquiti-community/unifi": {"resource_schemas": {
        "unifi_wireguard_peer": {"block": {"attributes": {
            "name":       {"type": "string", "required": True},
            "public_key": {"type": "string", "required": True},
        }}},
        **_SETTING_STUB,
    }}}}


def test_identity_wg_two_level_uses_composite():
    """_identity must return composite 'network_id:id' for wg_two_level state rows.

    The provider stores wireguard_peer state as {network_id: "NET", id: "PEER"};
    the enumerator emits import_id "NET:PEER". Before the fix, _identity returned
    bare "PEER" → new_targets never recognised managed peers → duplicates every run.
    """
    from ubitofu.pipeline import _identity  # type: ignore[attr-defined]

    result = _identity("wg_two_level", {"network_id": "NET1", "id": "PEER1",
                                        "name": "example_peer", "public_key": "KEY=="})
    assert result == "NET1:PEER1", f"expected composite 'NET1:PEER1', got {result!r}"


def test_reconcile_managed_wireguard_peer_not_reappended(monkeypatch, tmp_path):
    """Regression: a WireGuard peer already in state must not be re-appended.

    Full bug path:
    - enumerator emits composite import_id "NET1:PEER1" (name_hint "example_peer")
    - state row has network_id="NET1", id="PEER1", name="example_peer"
    - _state_addresses seeds reserved with "unifi_wireguard_peer.example_peer"
    - assign_slugs bumps to "example_peer_2" (example_peer is reserved)
    - tofu plan generates config for example_peer_2 → planned_values slug = "example_peer_2"
    - _identity bug: returns bare "PEER1" → new_targets classifies peer as new
    - peer appended to reconciled_new.tf as example_peer_2 on every run
    - fix: _identity returns "NET1:PEER1" → matched in state → not new → not appended

    RED before fix: reconciled_new.tf written with example_peer_2 peer block.
    GREEN after fix: reconciled_new.tf never created.
    """
    import ubitofu.pipeline as pl

    # State: peer is already managed. Provider stores network_id + bare id.
    state = {"values": {"root_module": {"resources": [
        {"type": "unifi_wireguard_peer", "name": "example_peer",
         "values": {"network_id": "NET1", "id": "PEER1",
                    "name": "example_peer", "public_key": "EXAMPLEKEY=="}},
    ]}}}

    # assign_slugs sees "unifi_wireguard_peer.example_peer" in reserved (from state)
    # and bumps the target's slug to "example_peer_2". The plan reflects this: tofu
    # generates config for "example_peer_2" (the import block says example_peer_2).
    plan = {
        "resource_changes": [],
        "planned_values": {"root_module": {"resources": [
            {"type": "unifi_wireguard_peer", "name": "example_peer_2",
             "values": {"name": "example_peer", "public_key": "EXAMPLEKEY=="}},
        ]}},
    }

    # Enumerator emits the composite import_id, exactly as _enumerate_wireguard does.
    targets = [ImportTarget("unifi_wireguard_peer", "example_peer", "NET1:PEER1")]

    class WGRunner(FakeRunner):
        def providers_schema(self):
            return _WG_SCHEMA

    monkeypatch.setattr(pl, "controller_from_config", lambda cfg: FakeCoverageController())
    monkeypatch.setattr(pl, "enumerate_controller",
                        lambda ctl: EnumerationResult(targets=targets, gaps=[]))
    monkeypatch.setattr(pl, "TofuRunner",
                        lambda workdir: WGRunner(workdir, plan, state))
    monkeypatch.setenv("UNIFI_API_KEY", "k")
    (tmp_path / "peers.tf").write_text(
        'resource "unifi_wireguard_peer" "example_peer" {\n'
        '  name       = "example_peer"\n'
        '  public_key = "REDACTED"\n'
        "}\n"
    )
    cfg = Config("https://unifi.example", "default", "env", "UNIFI_API_KEY",
                 "ExampleVault", workdir=str(tmp_path))
    out = io.StringIO()
    rc = pl.run_reconcile(cfg, out)

    assert rc == 0
    new_tf = tmp_path / "reconciled_new.tf"
    assert not new_tf.exists(), (
        "managed WireGuard peer wrongly re-appended as example_peer_2 — "
        f"reconciled_new.tf content:\n{new_tf.read_text()}"
    )


# ---------------------------------------------------------------------------
# Diverged classification: "gone on controller" vs "not yet applied". Both are
# plan `create` with before=None; only the live enumeration tells them apart.
# ---------------------------------------------------------------------------

COMMITTED_DEVICE_TF = '''resource "unifi_device" "example_ap" {
  mac  = "aa:bb:cc:00:00:01"
  name = "example AP"
}

resource "unifi_device" "example_ap_2" {
  mac  = "aa:bb:cc:00:00:02"
  name = "example AP 2"
}
'''


def _device_plan():
    return {
        "resource_changes": [
            {"type": "unifi_device", "name": "example_ap",
             "change": {"actions": ["create"], "before": None,
                        "after": {"mac": "aa:bb:cc:00:00:01", "name": "example AP"}}},
            {"type": "unifi_device", "name": "example_ap_2",
             "change": {"actions": ["create"], "before": None,
                        "after": {"mac": "aa:bb:cc:00:00:02", "name": "example AP 2"}}},
        ],
        "planned_values": {"root_module": {"resources": []}},
    }


def test_reconcile_device_gone_vs_pending(monkeypatch, tmp_path):
    """A live configured device is imported; an absent UI-only device forbids writes."""
    (tmp_path / "devices.tf").write_text(COMMITTED_DEVICE_TF)
    before = (tmp_path / "devices.tf").read_bytes()
    targets = [ImportTarget("unifi_device", "example_ap", "aa:bb:cc:00:00:01")]
    empty_state = {"values": {"root_module": {"resources": []}}}
    rc, report = _run(monkeypatch, tmp_path, _device_plan(), targets, empty_state)
    assert rc == 13
    assert "Forbidden (device create" in report
    assert "unifi_device.example_ap_2 — tofu can never create a device" in report
    assert "Imported into existing config:" in report
    assert "unifi_device.example_ap" in report
    assert (tmp_path / "devices.tf").read_bytes() == before
    assert not (tmp_path / "reconciled_new.tf").exists()


def test_reconcile_state_known_object_gone_is_deleted(monkeypatch, tmp_path):
    """A previously-applied _id-ruled resource (network) deleted out of band:
    the committed values carry no id, but the state row does — reconcile must
    still classify it as deleted, not "run apply", and now stages the block
    removal in the working tree rather than just flagging it (that behavior
    is superseded by staged deletions — see test_reconcile_stages_deletion_for_
    gone_network for the dedicated regression)."""
    _write_committed(tmp_path)
    plan = {
        "resource_changes": [
            {"type": "unifi_network", "name": "oldnet",
             "change": {"actions": ["create"], "before": None,
                        "after": {"name": "oldnet", "vlan": 66}}},
        ],
        "planned_values": {"root_module": {"resources": []}},
    }
    state = {"values": {"root_module": {"resources": [
        {"type": "unifi_network", "name": "examplenet", "values": {"id": "net001"}},
        {"type": "unifi_network", "name": "oldnet", "values": {"id": "net066"}},
    ]}}}
    targets = [ImportTarget("unifi_network", "examplenet", "net001")]
    _, report = _run(monkeypatch, tmp_path, plan, targets, state)
    assert "Removed (deleted on controller):" in report
    assert "unifi_network.oldnet" in report
    # the committed block is now staged for removal, replacing the old
    # flag-only behavior — the PR diff becomes the review surface.
    assert 'resource "unifi_network" "oldnet"' not in (
        tmp_path / "networks.tf").read_text()


# ---------------------------------------------------------------------------
# Existence automation: committed config is desired existence; plan/state/live
# only explain whether apply, import, staged removal, or attention comes next.
# ---------------------------------------------------------------------------

def test_forbidden_device_create_keeps_tree_byte_identical(monkeypatch, tmp_path):
    """One missing UI-only object forbids all writes, including a valid import."""
    (tmp_path / "devices.tf").write_text(COMMITTED_DEVICE_TF)
    before = _tree_snapshot(tmp_path)
    targets = [ImportTarget("unifi_device", "example AP", "aa:bb:cc:00:00:01")]
    empty_state = {"values": {"root_module": {"resources": []}}}
    rc, report = _run(monkeypatch, tmp_path, _device_plan(), targets, empty_state)
    assert rc == 13
    assert _tree_snapshot(tmp_path) == before
    assert "Imported into existing config:" in report
    assert "Forbidden (device create" in report


def test_forbidden_device_create_exits_13(monkeypatch, tmp_path):
    """A planned unifi_device create is a lifecycle violation: adoption is
    UI-only. 13 beats every other outcome so the gate is unambiguous.
    Note: staged deletion (Task 5) removes gone-device blocks, so this fires
    for what deletion cannot fix in-run — e.g. a device present live but
    uncaptured in state whose committed block would plan a create."""
    (tmp_path / "devices.tf").write_text(COMMITTED_DEVICE_TF)
    targets: list[ImportTarget] = []
    empty_state = {"values": {"root_module": {"resources": []}}}
    rc, report = _run(monkeypatch, tmp_path, _device_plan(), targets, empty_state)
    assert rc == 13
    assert "Forbidden (device create" in report
    assert "unifi_device.example_ap" in report


def test_configured_live_state_missing_emits_import_to_original_address(
        monkeypatch, tmp_path):
    (tmp_path / "clients.tf").write_text(
        'resource "unifi_client" "example_client" {\n'
        '  name = "example client"\n'
        '  mac  = "00:11:22:00:00:03"\n'
        "}\n"
    )
    plan = {
        "resource_changes": [
            {
                "address": "unifi_client.example_client",
                "type": "unifi_client",
                "name": "example_client",
                "change": {
                    "actions": ["create"],
                    "before": None,
                    "after": {
                        "name": "example client",
                        "mac": "00:11:22:00:00:03",
                    },
                },
            },
        ],
        "planned_values": {"root_module": {"resources": [
            {
                "address": "unifi_client.example_client_2",
                "type": "unifi_client",
                "name": "example_client_2",
                "values": {
                    "name": "example client",
                    "mac": "00:11:22:00:00:03",
                },
            },
        ]}},
    }
    targets = [ImportTarget(
        "unifi_client", "example client", "00:11:22:00:00:03")]
    empty_state = {"values": {"root_module": {"resources": []}}}

    rc, report = _run(monkeypatch, tmp_path, plan, targets, empty_state)

    emitted = (tmp_path / "reconciled_new.tf").read_text()
    assert 'to = unifi_client.example_client\n' in emitted
    assert 'id = "00:11:22:00:00:03"' in emitted
    assert 'resource "unifi_client"' not in emitted
    assert emitted.count("import {") == 1
    assert "Imported into existing config:" in report
    assert rc == 10

    rc2, _ = _run(monkeypatch, tmp_path, plan, targets, empty_state)
    assert (tmp_path / "reconciled_new.tf").read_text() == emitted
    assert rc2 == 0


def test_existing_generate_import_prevents_reconcile_duplicate(monkeypatch, tmp_path):
    (tmp_path / "generated.tf").write_text(
        'resource "unifi_client" "example_client" {\n'
        '  name = "example client"\n'
        '  mac  = "00:11:22:00:00:03"\n'
        "}\n"
    )
    imports = (
        "import {\n"
        "  to = unifi_client.example_client\n"
        '  id = "00:11:22:00:00:03"\n'
        "}\n"
    )
    (tmp_path / "imports.tf").write_text(imports)
    plan = {
        "resource_changes": [{
            "address": "unifi_client.example_client",
            "type": "unifi_client",
            "name": "example_client",
            "change": {
                "actions": ["create"],
                "before": None,
                "after": {"name": "example client", "mac": "00:11:22:00:00:03"},
            },
        }],
        "planned_values": {"root_module": {"resources": []}},
    }
    targets = [ImportTarget(
        "unifi_client", "example client", "00:11:22:00:00:03")]
    empty_state = {"values": {"root_module": {"resources": []}}}

    rc, report = _run(monkeypatch, tmp_path, plan, targets, empty_state)

    assert rc == 0
    assert "already in sync" in report
    assert (tmp_path / "imports.tf").read_text() == imports
    assert not (tmp_path / "reconciled_new.tf").exists()


@pytest.mark.parametrize(
    ("config", "address"),
    [
        (
            'module "example_edge" {\n  source = "./example"\n}\n',
            "module.example_edge.unifi_network.example_net",
        ),
        (
            'resource "unifi_network" "example_net" {\n'
            '  count = 1\n  name = "example net"\n}\n',
            "unifi_network.example_net[0]",
        ),
    ],
)
def test_full_address_managed_resources_are_not_false_invariants(
    monkeypatch, tmp_path, config, address
):
    (tmp_path / "main.tf").write_text(config)
    values = {"id": "00112233445566778899aabb", "name": "example net"}
    plan = {
        "resource_changes": [{
            "address": address,
            "type": "unifi_network",
            "name": "example_net",
            "change": {"actions": ["no-op"], "before": values, "after": values},
        }],
        "planned_values": {"root_module": {"resources": []}},
    }
    state = {"values": {"root_module": {"resources": [{
        "address": address,
        "type": "unifi_network",
        "name": "example_net",
        "values": values,
    }]}}}
    targets = [ImportTarget(
        "unifi_network", "example net", "00112233445566778899aabb")]

    rc, report = _run(monkeypatch, tmp_path, plan, targets, state)

    assert rc == 0
    assert "already in sync" in report
    assert "Requires attention (resource existence):" not in report


@pytest.mark.parametrize(
    ("config", "address"),
    [
        (
            'resource "unifi_network" "example_net" {\n'
            '  count = 1\n  name = "example net"\n}\n',
            "unifi_network.example_net[0]",
        ),
        (
            'resource "unifi_network" "example_net" {\n'
            '  for_each = { example = true }\n'
            '  name = "example net"\n}\n',
            'unifi_network.example_net["example"]',
        ),
        (
            'module "example_edge" {\n  source = "./example"\n}\n',
            "module.example_edge.unifi_network.example_net",
        ),
    ],
)
def test_missing_expanded_instance_never_deletes_entire_resource_block(
    monkeypatch, tmp_path, config, address
):
    path = tmp_path / "main.tf"
    path.write_text(config)
    before = path.read_bytes()
    values = {
        "id": "00112233445566778899aabb",
        "name": "example net",
    }
    plan = {
        "resource_changes": [{
            "address": address,
            "type": "unifi_network",
            "name": "example_net",
            "change": {"actions": ["create"], "before": None, "after": values},
        }],
        "planned_values": {"root_module": {"resources": []}},
    }
    state = {"values": {"root_module": {"resources": [{
        "address": address,
        "type": "unifi_network",
        "name": "example_net",
        "values": values,
    }]}}}

    rc, report = _run(monkeypatch, tmp_path, plan, [], state)

    assert path.read_bytes() == before
    assert rc == 11
    assert "expanded config address" in report
    assert "Removed (deleted on controller):" not in report


@pytest.mark.parametrize(
    ("config", "address"),
    [
        (
            'module "example_edge" {\n  source = "./example"\n}\n',
            "module.example_edge.unifi_client.example_client",
        ),
        (
            'resource "unifi_client" "example_client" {\n'
            '  for_each = { example = true }\n'
            '  name = "example client"\n'
            '  mac  = "00:11:22:00:00:03"\n}\n',
            'unifi_client.example_client["example"]',
        ),
    ],
)
def test_existing_live_full_address_import_preserves_target(
    monkeypatch, tmp_path, config, address
):
    (tmp_path / "main.tf").write_text(config)
    values = {"name": "example client", "mac": "00:11:22:00:00:03"}
    plan = {
        "resource_changes": [{
            "address": address,
            "type": "unifi_client",
            "name": "example_client",
            "change": {"actions": ["create"], "before": None, "after": values},
        }],
        "planned_values": {"root_module": {"resources": []}},
    }
    targets = [ImportTarget(
        "unifi_client", "example client", "00:11:22:00:00:03")]
    empty_state = {"values": {"root_module": {"resources": []}}}

    rc, report = _run(monkeypatch, tmp_path, plan, targets, empty_state)

    assert rc == 10
    emitted = (tmp_path / "reconciled_new.tf").read_text()
    assert f"to = {address}\n" in emitted
    assert "Imported into existing config:" in report


def test_ambiguous_configured_identity_suppresses_same_type_append(monkeypatch, tmp_path):
    (tmp_path / "networks.tf").write_text(
        'resource "unifi_network" "example_net" {\n'
        '  name = "example net"\n'
        "}\n"
    )
    plan = {
        "resource_changes": [
            {
                "address": "unifi_network.example_net",
                "type": "unifi_network",
                "name": "example_net",
                "change": {
                    "actions": ["create"],
                    "before": None,
                    "after": {"name": "example net"},
                },
            },
        ],
        "planned_values": {"root_module": {"resources": [
            {
                "address": "unifi_network.example_net_2",
                "type": "unifi_network",
                "name": "example_net_2",
                "values": {"name": "example net"},
            },
        ]}},
    }
    targets = [ImportTarget(
        "unifi_network", "example net", "00112233445566778899aabb")]
    empty_state = {"values": {"root_module": {"resources": []}}}

    rc, report = _run(monkeypatch, tmp_path, plan, targets, empty_state)

    assert not (tmp_path / "reconciled_new.tf").exists()
    assert "identity" in report.lower()
    assert "manual" in report.lower()
    assert rc == 11


@pytest.mark.parametrize(
    ("actions", "reason", "direction", "want_rc"),
    [
        (["forget"], None, "forget", 0),
        (["delete", "create"], None, None, 11),
        (["create", "delete"], None, None, 11),
        (["delete"], "delete_because_count_index", None, 11),
    ],
)
def test_state_only_plan_transition_reporting(
        monkeypatch, tmp_path, actions, reason, direction, want_rc):
    plan = {
        "resource_changes": [
            {
                "address": "unifi_network.example_state",
                "type": "unifi_network",
                "name": "example_state",
                "action_reason": reason,
                "change": {
                    "actions": actions,
                    "before": {"name": "example state"},
                    "after": None,
                },
            },
        ],
        "planned_values": {"root_module": {"resources": []}},
    }
    state = {"values": {"root_module": {"resources": [
        {
            "address": "unifi_network.example_state",
            "type": "unifi_network",
            "name": "example_state",
            "values": {"id": "00112233445566778899aabb"},
        },
    ]}}}

    rc, report = _run(monkeypatch, tmp_path, plan, [], state)

    assert rc == want_rc
    if direction is not None:
        assert f"unifi_network.example_state — {direction}" in report
        assert "Requires attention" not in report
    else:
        assert "Requires attention (resource existence):" in report


def test_reconcile_stages_deletion_for_gone_network(monkeypatch, tmp_path):
    """(L=0, S=1, C=1) stages deletion for ANY type, not just devices:
    oldnet was applied (state identity net066) and is gone live."""
    _write_committed(tmp_path)   # contains the oldnet block
    plan = {
        "resource_changes": [
            {"type": "unifi_network", "name": "oldnet",
             "change": {"actions": ["create"], "before": None,
                        "after": {"name": "oldnet", "vlan": 66}}},
        ],
        "planned_values": {"root_module": {"resources": []}},
    }
    state = {"values": {"root_module": {"resources": [
        {"type": "unifi_network", "name": "examplenet", "values": {"id": "net001"}},
        {"type": "unifi_network", "name": "oldnet", "values": {"id": "net066"}},
    ]}}}
    targets = [ImportTarget("unifi_network", "examplenet", "net001")]
    rc, report = _run(monkeypatch, tmp_path, plan, targets, state)
    text = (tmp_path / "networks.tf").read_text()
    assert 'resource "unifi_network" "oldnet"' not in text
    assert "Removed (deleted on controller):" in report
    assert rc in (10, 12)


def test_reconcile_live_state_only_missing_config_is_pending_destroy(monkeypatch, tmp_path):
    """Live state is current existence, not permission to recreate missing config."""
    _write_committed(tmp_path)
    plan = {
        "resource_changes": [
            # orphan: in state, no committed block -> plan wants to delete it
            {"type": "unifi_network", "name": "statenet",
             "action_reason": "delete_because_no_resource_config",
             "change": {"actions": ["delete"],
                        "before": {"name": "statenet", "vlan": 30,
                                   "mtu": 1500, "enabled": True,
                                   "dhcp_server": None},
                        "after": None}},
        ],
        "planned_values": {"root_module": {"resources": []}},
    }
    state = {"values": {"root_module": {"resources": [
        {"type": "unifi_network", "name": "examplenet", "values": {"id": "net001"}},
        {"type": "unifi_network", "name": "statenet", "values": {"id": "net030"}},
    ]}}}
    targets = [
        ImportTarget("unifi_network", "examplenet", "net001"),
        ImportTarget("unifi_network", "statenet", "net030"),   # live!
    ]
    rc, report = _run(monkeypatch, tmp_path, plan, targets, state)
    assert not (tmp_path / "reconciled_new.tf").exists()
    assert "Pending apply (config intent not yet applied):" in report
    assert "unifi_network.statenet — destroy" in report
    assert "Codified" not in report
    assert rc == 0


def test_reconcile_state_only_secret_is_not_codified(monkeypatch, tmp_path):
    """State-only removal intent never writes resource or secret-variable HCL."""
    import ubitofu.pipeline as pl

    wlan_schema = {"provider_schemas": {
        "registry.opentofu.org/ubiquiti-community/unifi": {"resource_schemas": {
            "unifi_wlan": {"block": {"attributes": {
                "name":       {"type": "string", "required": True},
                "passphrase": {"type": "string", "optional": True, "sensitive": True},
                "security":   {"type": "string", "optional": True},
            }}},
            **_SETTING_STUB,
        }}}}

    plan = {
        "resource_changes": [
            # orphan: in state, live, no committed block -> plan wants to delete it
            {"type": "unifi_wlan", "name": "statewlan",
             "action_reason": "delete_because_no_resource_config",
             "change": {"actions": ["delete"],
                        "before": {"name": "statewlan",
                                   "passphrase": "REDACTED",
                                   "security": "wpapsk"},
                        "after": None}},
        ],
        "planned_values": {"root_module": {"resources": []}},
    }
    targets = [ImportTarget("unifi_wlan", "statewlan", "wlan030")]
    state = {"values": {"root_module": {"resources": [
        {"type": "unifi_wlan", "name": "statewlan", "values": {"id": "wlan030"}},
    ]}}}

    class WlanRunner(FakeRunner):
        def providers_schema(self):
            return wlan_schema

    monkeypatch.setattr(pl, "controller_from_config", lambda cfg: FakeCoverageController())
    monkeypatch.setattr(pl, "enumerate_controller",
                        lambda ctl: EnumerationResult(targets=targets, gaps=[]))
    monkeypatch.setattr(pl, "TofuRunner",
                        lambda workdir: WlanRunner(workdir, plan, state))
    monkeypatch.setenv("UNIFI_API_KEY", "k")
    cfg = Config("https://unifi.example", "default", "env", "UNIFI_API_KEY",
                 "ExampleVault", workdir=str(tmp_path))
    out = io.StringIO()
    rc = pl.run_reconcile(cfg, out)
    report = out.getvalue()

    assert not (tmp_path / "reconciled_new.tf").exists()
    assert not (tmp_path / "unifi-variables.tf").exists()
    assert "unifi_wlan.statewlan — destroy" in report
    assert "TF_VAR" not in report
    assert rc == 0


def test_reconcile_dead_orphan_keeps_destroy_advisory(monkeypatch, tmp_path):
    """In state, absent live and from config: next apply forgets it — the
    advisory stays and reconcile writes nothing."""
    _write_committed(tmp_path)
    plan = {
        "resource_changes": [
            {"type": "unifi_network", "name": "statenet",
             "action_reason": "delete_because_no_resource_config",
             "change": {"actions": ["delete"],
                        "before": {"name": "statenet", "vlan": 30},
                        "after": None}},
        ],
        "planned_values": {"root_module": {"resources": []}},
    }
    state = {"values": {"root_module": {"resources": [
        {"type": "unifi_network", "name": "statenet", "values": {"id": "net030"}},
    ]}}}
    targets = []          # not live
    rc, report = _run(monkeypatch, tmp_path, plan, targets, state)
    assert "unifi_network.statenet — destroy" in report
    assert not (tmp_path / "reconciled_new.tf").exists()
    assert rc == 0


# ---------------------------------------------------------------------------
# Outcome exit codes — scriptable without grepping the report:
# rsync-style flat codes: 0 in sync, 10 drift captured, 11 attention, 12 both.
# ---------------------------------------------------------------------------

def _merge_only_plan():
    vals = {"name": "examplenet", "vlan": 20, "mtu": 1500, "enabled": True,
            "dhcp_server": {"enabled": True, "start": "10.0.0.10"}}
    return {
        "resource_changes": [
            {"type": "unifi_network", "name": "examplenet",
             "change": {"actions": ["update"],
                        "before": vals,                       # LIVE
                        "after": {**vals, "vlan": 10}}},      # COMMITTED
        ],
        "planned_values": {"root_module": {"resources": []}},
    }


def test_reconcile_exit_0_when_in_sync(monkeypatch, tmp_path):
    _write_committed(tmp_path)
    plan = {"resource_changes": [],
            "planned_values": {"root_module": {"resources": []}}}
    targets = [ImportTarget("unifi_network", "examplenet", "net001")]
    rc, _ = _run(monkeypatch, tmp_path, plan, targets, STATE)
    assert rc == 0


def test_reconcile_exit_captured_bit_when_drift_captured_only(monkeypatch, tmp_path):
    _write_committed(tmp_path)
    targets = [ImportTarget("unifi_network", "examplenet", "net001")]
    rc, report = _run(monkeypatch, tmp_path, _merge_only_plan(), targets, STATE)
    assert "Auto-merged" in report
    assert rc == 10


def test_reconcile_pending_create_intent_exits_zero(monkeypatch, tmp_path):
    """A merged-but-unapplied new creatable resource ("pending" tag) must not
    set the attention bit: reconcile can never clear a pending create, only
    `apply` can, so pending is convergent for the gate — exit 0. The report
    still names the resource (`not yet applied`) so the operator knows apply
    is expected to run next; it just no longer blocks that apply.

    Uses unifi_network, not unifi_device: since Task 7, any unifi_device
    create not staged for deletion also trips the forbidden-create gate
    (exit 13), which is a different, still-blocking outcome (see
    test_forbidden_device_create_exits_13). Exit-11 coverage for a genuine
    blocking flag remains covered by the state-only invariant cases.

    ``somenet``'s identity is underivable pre-apply (no "id" in committed
    values yet, so no live identity can be joined), which is what makes it
    "pending" regardless of live status. targets stays empty so the
    live-enumeration new-object loop (a separate mechanism, unrelated to
    this classification) never also flags it, keeping the pending tag the
    sole contributor to this run's outcome."""
    (tmp_path / "networks.tf").write_text(
        'resource "unifi_network" "somenet" {\n'
        '  name = "somenet"\n'
        "}\n")
    plan = {
        "resource_changes": [
            {"type": "unifi_network", "name": "somenet",
             "change": {"actions": ["create"], "before": None,
                        "after": {"name": "somenet"}}},
        ],
        "planned_values": {"root_module": {"resources": []}},
    }
    targets: list[ImportTarget] = []
    empty_state = {"values": {"root_module": {"resources": []}}}
    rc, report = _run(monkeypatch, tmp_path, plan, targets, empty_state)
    assert rc == 0
    assert "not yet applied" in report


def _pending_create_change():
    return {
        "address": "unifi_network.example_pending",
        "type": "unifi_network",
        "name": "example_pending",
        "change": {
            "actions": ["create"],
            "before": None,
            "after": {"name": "example pending"},
        },
    }


def _state_only_replace_change():
    return {
        "address": "unifi_network.example_state",
        "type": "unifi_network",
        "name": "example_state",
        "change": {
            "actions": ["delete", "create"],
            "before": {"name": "example state"},
            "after": {"name": "example state"},
        },
    }


def _write_pending_config(tmp_path):
    (tmp_path / "pending.tf").write_text(
        'resource "unifi_network" "example_pending" {\n'
        '  name = "example pending"\n'
        "}\n"
    )


def test_reconcile_captured_plus_pending_exits_10(monkeypatch, tmp_path):
    _write_committed(tmp_path)
    _write_pending_config(tmp_path)
    plan = _merge_only_plan()
    plan["resource_changes"].append(_pending_create_change())
    targets = [ImportTarget("unifi_network", "examplenet", "net001")]

    rc, report = _run(monkeypatch, tmp_path, plan, targets, STATE)

    assert "Auto-merged" in report
    assert "unifi_network.example_pending — create" in report
    assert rc == 10


def test_reconcile_attention_plus_pending_exits_11(monkeypatch, tmp_path):
    _write_pending_config(tmp_path)
    plan = {
        "resource_changes": [_pending_create_change(), _state_only_replace_change()],
        "planned_values": {"root_module": {"resources": []}},
    }
    state = {"values": {"root_module": {"resources": [
        {"address": "unifi_network.example_state", "type": "unifi_network",
         "name": "example_state", "values": {"id": "00112233445566778899aabb"}},
    ]}}}

    rc, report = _run(monkeypatch, tmp_path, plan, [], state)

    assert "unifi_network.example_pending — create" in report
    assert "replacement requires manual review" in report
    assert rc == 11


def test_reconcile_captured_attention_plus_pending_exits_12(monkeypatch, tmp_path):
    _write_committed(tmp_path)
    _write_pending_config(tmp_path)
    plan = _merge_only_plan()
    plan["resource_changes"].extend([
        _pending_create_change(),
        _state_only_replace_change(),
    ])
    state = {"values": {"root_module": {"resources": [
        *STATE["values"]["root_module"]["resources"],
        {"address": "unifi_network.example_state", "type": "unifi_network",
         "name": "example_state", "values": {"id": "00112233445566778899aabb"}},
    ]}}}
    targets = [ImportTarget("unifi_network", "examplenet", "net001")]

    rc, report = _run(monkeypatch, tmp_path, plan, targets, state)

    assert "Auto-merged" in report
    assert "unifi_network.example_pending — create" in report
    assert "replacement requires manual review" in report
    assert rc == 12


# ---------------------------------------------------------------------------
# Three-way semantics: state (last applied) disambiguates controller drift
# from unapplied config intent. The intent-preservation case is the mirror
# image of the hide_ssid incident: reconcile must never revert a deliberate
# committed change that simply has not been applied yet.
# ---------------------------------------------------------------------------

def _threeway_state(vlan):
    return {"values": {"root_module": {"resources": [
        {"type": "unifi_network", "name": "examplenet",
         "values": {"id": "net001", "name": "examplenet", "vlan": vlan,
                    "mtu": 1500, "enabled": True,
                    "dhcp_server": {"enabled": True, "start": "10.0.0.10"}}},
    ]}}}


def _threeway_plan(live_vlan, committed_vlan):
    vals = {"name": "examplenet", "mtu": 1500, "enabled": True,
            "dhcp_server": {"enabled": True, "start": "10.0.0.10"}}
    return {
        "resource_changes": [
            {"type": "unifi_network", "name": "examplenet",
             "change": {"actions": ["update"],
                        "before": {**vals, "vlan": live_vlan},
                        "after": {**vals, "vlan": committed_vlan}}},
        ],
        "planned_values": {"root_module": {"resources": []}},
    }


def test_threeway_intent_preserved(monkeypatch, tmp_path):
    """Committed vlan 10, last applied 20, live 20: the config change is
    unapplied INTENT. Reconcile must leave the file alone and exit 0."""
    _write_committed(tmp_path)   # committed vlan is 10
    targets = [ImportTarget("unifi_network", "examplenet", "net001")]
    rc, report = _run(monkeypatch, tmp_path,
                      _threeway_plan(live_vlan=20, committed_vlan=10),
                      targets, _threeway_state(vlan=20))
    text = (tmp_path / "networks.tf").read_text()
    assert "vlan    = 10 # pinned VLAN, keep this comment" in text  # untouched
    assert rc == 0
    assert "Auto-merged" not in report


def test_threeway_drift_still_captured(monkeypatch, tmp_path):
    """Committed 10, last applied 10, live 20: controller drift — capture."""
    _write_committed(tmp_path)
    targets = [ImportTarget("unifi_network", "examplenet", "net001")]
    rc, report = _run(monkeypatch, tmp_path,
                      _threeway_plan(live_vlan=20, committed_vlan=10),
                      targets, _threeway_state(vlan=10))
    text = (tmp_path / "networks.tf").read_text()
    assert "vlan    = 20 # pinned VLAN, keep this comment" in text
    assert rc == 10


def test_threeway_conflict_flagged(monkeypatch, tmp_path):
    """Live 30, last applied 20, committed 10: changed on both sides."""
    _write_committed(tmp_path)
    targets = [ImportTarget("unifi_network", "examplenet", "net001")]
    rc, report = _run(monkeypatch, tmp_path,
                      _threeway_plan(live_vlan=30, committed_vlan=10),
                      targets, _threeway_state(vlan=20))
    text = (tmp_path / "networks.tf").read_text()
    assert "vlan    = 10" in text                     # nothing auto-edited
    assert "conflict" in report.lower()
    assert ("unifi_network.examplenet.vlan: conflict — live 30, "
            "last applied 20, committed 10 — manual review") in report
    assert rc == 11


# ---------------------------------------------------------------------------
# Provider-default drift: `change.after` is the PLANNED value, not the config.
# For an attribute the committed HCL never declares, "after" is whatever the
# provider defaulted — so the three-way state logic must not read it as
# unapplied config intent. Live wins, and the only way to hold it is to write
# the attribute into the block. (Real case: ubiquiti 0.101.0 gave
# unifi_wlan.roaming_assistant_na_enabled a static `false` default, planning
# the assistant off on every WLAN that had it on.)
# ---------------------------------------------------------------------------

def _default_plan(live_value, planned_value):
    vals = {"name": "examplenet", "vlan": 10, "mtu": 1500, "enabled": True,
            "dhcp_server": {"enabled": True, "start": "10.0.0.10"}}
    return {
        "resource_changes": [
            {"type": "unifi_network", "name": "examplenet",
             "change": {"actions": ["update"],
                        "before": {**vals, "mdns": live_value},
                        "after": {**vals, "mdns": planned_value}}},
        ],
        "planned_values": {"root_module": {"resources": []}},
    }


def _default_state(**extra):
    return {"values": {"root_module": {"resources": [
        {"type": "unifi_network", "name": "examplenet",
         "values": {"id": "net001", "name": "examplenet", "vlan": 10,
                    "mtu": 1500, "enabled": True,
                    "dhcp_server": {"enabled": True, "start": "10.0.0.10"},
                    **extra}},
    ]}}}


def test_provider_default_over_live_is_captured_not_read_as_intent(monkeypatch, tmp_path):
    """State carries the live value, so the two-way `committed moved, live ==
    state` test looks exactly like unapplied intent — but the committed HCL
    never mentions the attribute, so there is no intent to preserve. Codify
    live instead of letting apply write the provider's default."""
    _write_committed(tmp_path)
    targets = [ImportTarget("unifi_network", "examplenet", "net001")]
    rc, report = _run(monkeypatch, tmp_path,
                      _default_plan(live_value=True, planned_value=False),
                      targets, _default_state(mdns=True))
    text = (tmp_path / "networks.tf").read_text()
    assert "mdns = true" in text
    assert "unifi_network.examplenet.mdns" in report
    assert rc == 10


def test_provider_default_captured_when_state_lacks_the_attr(monkeypatch, tmp_path):
    """Same shape, but the attribute is absent from state (it did not exist in
    the provider version that last applied). Falls back to the two-way path,
    which used to die in update_scalar with "could not edit in place"."""
    _write_committed(tmp_path)
    targets = [ImportTarget("unifi_network", "examplenet", "net001")]
    rc, report = _run(monkeypatch, tmp_path,
                      _default_plan(live_value=True, planned_value=False),
                      targets, _default_state())
    text = (tmp_path / "networks.tf").read_text()
    assert "mdns = true" in text
    assert "could not" not in report
    assert rc == 10


def test_provider_default_insert_keeps_the_rest_byte_identical(monkeypatch, tmp_path):
    """The inserted assignment is the only edit: comments, alignment and the
    nested block survive, and the second resource is untouched."""
    _write_committed(tmp_path)
    targets = [ImportTarget("unifi_network", "examplenet", "net001")]
    _run(monkeypatch, tmp_path,
         _default_plan(live_value=True, planned_value=False),
         targets, _default_state(mdns=True))
    text = (tmp_path / "networks.tf").read_text()
    assert text.replace("  mdns = true\n", "") == COMMITTED_NETWORK_TF


def test_declared_attr_still_reads_as_unapplied_intent(monkeypatch, tmp_path):
    """The guard is narrow: an attribute the operator DID declare keeps the
    old three-way behaviour, so a deliberate unapplied edit is never reverted
    (the hide_ssid incident). vlan is declared as 10; live and state agree on
    20; reconcile leaves it alone."""
    _write_committed(tmp_path)
    targets = [ImportTarget("unifi_network", "examplenet", "net001")]
    rc, _ = _run(monkeypatch, tmp_path,
                 _threeway_plan(live_vlan=20, committed_vlan=10),
                 targets, _threeway_state(vlan=20))
    text = (tmp_path / "networks.tf").read_text()
    assert "vlan    = 10 # pinned VLAN, keep this comment" in text
    assert rc == 0


def test_provider_default_check_mode_writes_nothing(monkeypatch, tmp_path):
    """--check must classify the insert without touching the tree."""
    import ubitofu.pipeline as pl
    _write_committed(tmp_path)
    before = (tmp_path / "networks.tf").read_bytes()
    targets = [ImportTarget("unifi_network", "examplenet", "net001")]
    monkeypatch.setattr(pl, "controller_from_config", lambda cfg: FakeCoverageController())
    monkeypatch.setattr(pl, "enumerate_controller",
                        lambda ctl: EnumerationResult(targets=targets, gaps=[]))
    monkeypatch.setattr(pl, "TofuRunner", lambda workdir: FakeRunner(
        workdir, _default_plan(live_value=True, planned_value=False),
        _default_state(mdns=True)))
    monkeypatch.setenv("UNIFI_API_KEY", "k")
    cfg = Config("https://unifi.example", "default", "env", "UNIFI_API_KEY",
                 "ExampleVault", workdir=str(tmp_path))
    out = io.StringIO()
    rc = pl.run_reconcile(cfg, out, check=True)
    assert (tmp_path / "networks.tf").read_bytes() == before
    assert rc == 10
    assert "unifi_network.examplenet.mdns" in out.getvalue()


def _tree_snapshot(root):
    return {p: p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


def test_check_mode_writes_nothing_same_exit(monkeypatch, tmp_path):
    """--check returns the same exit code as a wet run but leaves the tree
    byte-identical — the apply gate depends on that. The plan also carries a
    gone-applied network (staged-deletion guard) and a state-only invariant,
    so both staged mutation and attention paths are exercised."""
    import ubitofu.pipeline as pl
    _write_committed(tmp_path)
    (tmp_path / "goneapplied.tf").write_text(
        'resource "unifi_network" "goneapplied" {\n'
        '  name = "goneapplied"\n'
        "  vlan = 77\n"
        "}\n")
    targets = [*_drift_targets(),   # scalar drift + a new object: wet run would edit + append
               ImportTarget("unifi_network", "stateorphan", "net088")]  # live orphan
    plan = _drift_plan()
    plan["resource_changes"].append(
        {"type": "unifi_network", "name": "goneapplied",
         "change": {"actions": ["create"], "before": None,
                    "after": {"name": "goneapplied", "vlan": 77}}})
    plan["resource_changes"].append(
        {"type": "unifi_network", "name": "stateorphan",
         "action_reason": "delete_because_count_index",
         "change": {"actions": ["delete"],
                    "before": {"name": "stateorphan", "vlan": 88,
                               "mtu": 1500, "enabled": True,
                               "dhcp_server": None},
                    "after": None}})
    state = {"values": {"root_module": {"resources": [
        *STATE["values"]["root_module"]["resources"],
        {"type": "unifi_network", "name": "goneapplied", "values": {"id": "net077"}},
        {"type": "unifi_network", "name": "stateorphan", "values": {"id": "net088"}},
    ]}}}
    monkeypatch.setattr(pl, "controller_from_config", lambda cfg: FakeCoverageController())
    monkeypatch.setattr(pl, "enumerate_controller",
                        lambda ctl: EnumerationResult(targets=targets, gaps=[]))
    monkeypatch.setattr(pl, "TofuRunner",
                        lambda workdir: FakeRunner(workdir, plan, state))
    monkeypatch.setenv("UNIFI_API_KEY", "k")
    cfg = Config("https://unifi.example", "default", "env", "UNIFI_API_KEY",
                 "ExampleVault", workdir=str(tmp_path))
    before = _tree_snapshot(tmp_path)
    out = io.StringIO()
    rc = pl.run_reconcile(cfg, out, check=True)
    assert _tree_snapshot(tmp_path) == before
    assert rc in (10, 12)
    report = out.getvalue()
    assert "Auto-merged" in report            # report still names the capture
    assert "Removed (deleted on controller):" in report   # staged-deletion guard exercised
    assert "Requires attention (resource existence):" in report


def test_wet_and_check_mode_make_identical_existence_decisions(monkeypatch, tmp_path):
    import ubitofu.pipeline as pl

    wet = tmp_path / "wet"
    dry = tmp_path / "dry"
    wet.mkdir()
    dry.mkdir()
    config = (
        'resource "unifi_client" "example_client" {\n'
        '  name = "example client"\n'
        '  mac  = "00:11:22:00:00:03"\n'
        "}\n"
    )
    (wet / "clients.tf").write_text(config)
    (dry / "clients.tf").write_text(config)
    plan = {
        "resource_changes": [{
            "address": "unifi_client.example_client",
            "type": "unifi_client",
            "name": "example_client",
            "change": {
                "actions": ["create"],
                "before": None,
                "after": {"name": "example client", "mac": "00:11:22:00:00:03"},
            },
        }],
        "planned_values": {"root_module": {"resources": [{
            "address": "unifi_client.example_client_2",
            "type": "unifi_client",
            "name": "example_client_2",
            "values": {"name": "example client", "mac": "00:11:22:00:00:03"},
        }]}},
    }
    state = {"values": {"root_module": {"resources": []}}}
    targets = [ImportTarget(
        "unifi_client", "example client", "00:11:22:00:00:03")]
    monkeypatch.setattr(pl, "controller_from_config", lambda cfg: FakeCoverageController())
    monkeypatch.setattr(pl, "enumerate_controller",
                        lambda ctl: EnumerationResult(targets=targets, gaps=[]))
    monkeypatch.setattr(pl, "TofuRunner",
                        lambda workdir: FakeRunner(workdir, plan, state))
    monkeypatch.setenv("UNIFI_API_KEY", "k")

    wet_out = io.StringIO()
    wet_rc = pl.run_reconcile(
        Config("https://unifi.example", "default", "env", "UNIFI_API_KEY",
               "ExampleVault", workdir=str(wet)),
        wet_out,
    )
    dry_before = _tree_snapshot(dry)
    dry_out = io.StringIO()
    dry_rc = pl.run_reconcile(
        Config("https://unifi.example", "default", "env", "UNIFI_API_KEY",
               "ExampleVault", workdir=str(dry)),
        dry_out,
        check=True,
    )

    assert wet_rc == dry_rc == 10
    assert wet_out.getvalue() == dry_out.getvalue()
    assert _tree_snapshot(dry) == dry_before
    assert "Imported into existing config:" in dry_out.getvalue()


def _gone_device_fixture(tmp_path, with_group_ref):
    (tmp_path / "devices.tf").write_text(
        'resource "unifi_device" "example_ap_2" {\n'
        "  mac  = \"aa:bb:cc:00:00:02\"\n"
        "  name = \"example AP 2\"\n"
        "}\n"
    )
    if with_group_ref:
        (tmp_path / "groups.tf").write_text(
            'resource "unifi_ap_group" "inside" {\n'
            "  device_macs = [\n"
            "    unifi_device.example_ap_2.mac,\n"
            "  ]\n"
            "}\n"
        )
    plan = {
        "resource_changes": [
            {"type": "unifi_device", "name": "example_ap_2",
             "change": {"actions": ["create"], "before": None,
                        "after": {"mac": "aa:bb:cc:00:00:02",
                                  "name": "example AP 2"}}},
        ],
        "planned_values": {"root_module": {"resources": []}},
    }
    return plan


def test_staged_deletion_reports_dangling_references(monkeypatch, tmp_path):
    """Deleting a block whose address other config still references must
    name each dangler (file:line) and hold the attention bit — merging the
    drift PR as-is would fail validate on the dangling expression."""
    plan = _gone_device_fixture(tmp_path, with_group_ref=True)
    state = {"values": {"root_module": {"resources": [
        {"type": "unifi_device", "name": "example_ap_2",
         "values": {"id": "aa:bb:cc:00:00:02", "mac": "aa:bb:cc:00:00:02"}},
    ]}}}
    rc, report = _run(monkeypatch, tmp_path, plan, [], state)
    assert 'resource "unifi_device" "example_ap_2"' not in (tmp_path / "devices.tf").read_text()
    assert "unifi_device.example_ap_2: still referenced at groups.tf:3" in report
    assert rc == 12          # deletion captured + dangler needs attention


def test_staged_deletion_without_references_stays_captured_only(monkeypatch, tmp_path):
    plan = _gone_device_fixture(tmp_path, with_group_ref=False)
    state = {"values": {"root_module": {"resources": [
        {"type": "unifi_device", "name": "example_ap_2",
         "values": {"id": "aa:bb:cc:00:00:02", "mac": "aa:bb:cc:00:00:02"}},
    ]}}}
    rc, report = _run(monkeypatch, tmp_path, plan, [], state)
    assert "still referenced" not in report
    assert rc == 10
