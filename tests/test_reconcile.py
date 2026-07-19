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

    def collection(self, endpoint):
        return []


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

    monkeypatch.setattr(pl, "Controller", lambda **kw: FakeCoverageController())
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

    monkeypatch.setattr(pl, "Controller", lambda **kw: FakeCoverageController())
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

    monkeypatch.setattr(pl, "Controller", lambda **kw: FakeCoverageController())
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

    monkeypatch.setattr(pl, "Controller", lambda **kw: FakeCoverageController())
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


def test_reconcile_flags_orphaned_state_resource(monkeypatch, tmp_path, fixtures_dir):
    """A resource in state but absent from committed config with a destructive
    action (delete/replace/create) must appear in the report as would-be-DESTROYED,
    never silently ignored."""
    import json
    plan = json.loads((fixtures_dir / "reconcile" / "plan_orphan.json").read_text())
    plan.setdefault("planned_values", {"root_module": {"resources": []}})
    _write_committed(tmp_path)
    # targets just need to be non-empty; the orphan resource_change drives the test
    targets = [ImportTarget("unifi_network", "examplenet", "net001")]
    rc, report = _run(monkeypatch, tmp_path, plan, targets, STATE)
    assert rc == 11      # orphan flagged, nothing captured
    assert "example_fwd" in report
    assert "DESTROY" in report.upper()
    assert report.count("would be DESTROYED on apply") == 1


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

    monkeypatch.setattr(pl, "Controller", lambda **kw: FakeCoverageController())
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

    monkeypatch.setattr(pl, "Controller", lambda **kw: FakeCoverageController())
    monkeypatch.setattr(pl, "enumerate_controller",
                        lambda ctl: EnumerationResult(targets=targets, gaps=[]))
    monkeypatch.setattr(pl, "TofuRunner",
                        lambda workdir: WGRunner(workdir, plan, state))
    monkeypatch.setenv("UNIFI_API_KEY", "k")
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
    """example_ap still exists on the controller (merged, apply pending); example_ap_2
    was removed in the UI. The report must send the operator down different
    paths: example_ap trips the forbidden-create gate (a device's planned
    create is always a lifecycle violation — reconcile cannot apply it, so a
    contradictory "run apply" pending line must not also appear for it),
    staged block removal for example_ap_2 (a device tofu can never create,
    so "run apply" cannot adopt it — the block is now deleted from the
    working tree instead of merely flagged)."""
    (tmp_path / "devices.tf").write_text(COMMITTED_DEVICE_TF)
    targets = [ImportTarget("unifi_device", "example_ap", "aa:bb:cc:00:00:01")]
    empty_state = {"values": {"root_module": {"resources": []}}}
    rc, report = _run(monkeypatch, tmp_path, _device_plan(), targets, empty_state)
    assert rc == 13
    assert "unifi_device.example_ap — in config, not yet applied — run apply" not in report
    assert "Forbidden (device create" in report
    assert "unifi_device.example_ap — tofu can never create a device" in report
    assert "Removed (deleted on controller):" in report
    assert "unifi_device.example_ap_2" in report
    text = (tmp_path / "devices.tf").read_text()
    assert 'resource "unifi_device" "example_ap_2"' not in text
    assert 'resource "unifi_device" "example_ap"' in text


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
# Existence automation: applied-then-deleted objects get their blocks staged
# for removal; live state-only orphans get codified instead of destroyed.
# ---------------------------------------------------------------------------

def test_reconcile_stages_deletion_for_gone_device(monkeypatch, tmp_path):
    """example_ap_2 (absent live) is deleted; example_ap (present) survives
    the staged deletion — but it is still a planned create tofu can never
    execute, so it now trips the Task 7 forbidden-create gate (exit 13)
    rather than the pre-Task-7 drift-captured outcome."""
    (tmp_path / "devices.tf").write_text(COMMITTED_DEVICE_TF)
    targets = [ImportTarget("unifi_device", "example AP", "aa:bb:cc:00:00:01")]
    empty_state = {"values": {"root_module": {"resources": []}}}
    rc, report = _run(monkeypatch, tmp_path, _device_plan(), targets, empty_state)
    text = (tmp_path / "devices.tf").read_text()
    assert 'resource "unifi_device" "example_ap_2"' not in text
    assert 'resource "unifi_device" "example_ap"' in text
    assert "Removed (deleted on controller):" in report
    assert rc == 13


def test_forbidden_device_create_exits_13(monkeypatch, tmp_path):
    """A planned unifi_device create is a lifecycle violation: adoption is
    UI-only. 13 beats every other outcome so the gate is unambiguous.
    Note: staged deletion (Task 5) removes gone-device blocks, so this fires
    for what deletion cannot fix in-run — e.g. a device present live but
    uncaptured in state whose committed block would plan a create."""
    (tmp_path / "devices.tf").write_text(COMMITTED_DEVICE_TF)
    # both devices live -> classify says pending; plan still says create
    targets = [ImportTarget("unifi_device", "example AP", "aa:bb:cc:00:00:01"),
               ImportTarget("unifi_device", "example AP 2", "aa:bb:cc:00:00:02")]
    empty_state = {"values": {"root_module": {"resources": []}}}
    rc, report = _run(monkeypatch, tmp_path, _device_plan(), targets, empty_state)
    assert rc == 13
    assert "Forbidden (device create" in report
    assert "unifi_device.example_ap" in report


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


def test_reconcile_codifies_live_state_orphan(monkeypatch, tmp_path):
    """In state and live but never committed (the state-only-orphan cell): append the
    block from live values instead of warning about destruction."""
    _write_committed(tmp_path)
    plan = {
        "resource_changes": [
            # orphan: in state, no committed block -> plan wants to delete it
            {"type": "unifi_network", "name": "statenet",
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
    new_tf = (tmp_path / "reconciled_new.tf").read_text()
    assert 'resource "unifi_network" "statenet"' in new_tf
    assert "import {" not in new_tf          # already in state — no import block
    assert "Codified (state-only → config):" in report
    assert "would be DESTROYED" not in report
    assert rc in (10, 12)


def test_reconcile_codified_secret_orphan_declares_variable(monkeypatch, tmp_path):
    """A live state-only orphan (the codification path) whose plan `before`
    carries a sensitive attribute must declare its secret variable, exactly
    like a newly-appended secret-bearing object does. The codification
    branch reuses build_resource_attrs, whose cleaner turns the sensitive
    value into a VarRef — but unlike the appended-objects loop, it never
    scanned attrs for VarRefs into secret_var_names, so the codified block
    referenced an undeclared var.<name> with no TF_VAR warning."""
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
             "change": {"actions": ["delete"],
                        "before": {"name": "statewlan",
                                   "passphrase": "live-secret-xyz",
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

    monkeypatch.setattr(pl, "Controller", lambda **kw: FakeCoverageController())
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

    new_tf = (tmp_path / "reconciled_new.tf").read_text()
    assert 'resource "unifi_wlan" "statewlan"' in new_tf
    assert "live-secret-xyz" not in new_tf          # no plaintext secret ever written
    assert "var.wlan_statewlan_psk" in new_tf
    variables_tf = (tmp_path / "unifi-variables.tf").read_text()
    assert "sensitive = true" in variables_tf
    assert "TF_VAR_wlan_statewlan_psk" in report
    assert rc == 12      # codified captured AND secret var to declare


def test_reconcile_dead_orphan_keeps_destroy_advisory(monkeypatch, tmp_path):
    """In state, absent live and from config: next apply forgets it — the
    advisory stays, nothing is codified."""
    _write_committed(tmp_path)
    plan = {
        "resource_changes": [
            {"type": "unifi_network", "name": "statenet",
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
    assert "would be DESTROYED" in report
    assert not (tmp_path / "reconciled_new.tf").exists()


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
    blocking flag remains via test_reconcile_flags_orphaned_state_resource.

    ``somenet``'s identity is underivable pre-apply (no "id" in committed
    values yet — classify_diverged's conservative fallback), which is what
    makes it "pending" regardless of live status; targets stays empty so the
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


def _tree_snapshot(root):
    return {p: p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


def test_check_mode_writes_nothing_same_exit(monkeypatch, tmp_path):
    """--check returns the same exit code as a wet run but leaves the tree
    byte-identical — it is the apply gate's oracle. The plan also carries a
    gone-applied network (staged-deletion guard: would rewrite the committed
    block) and a live state-only orphan (codification guard: would write
    reconciled_new.tf) so both write-skipping branches are exercised, not
    just the scalar-merge / append paths."""
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
    monkeypatch.setattr(pl, "Controller", lambda **kw: FakeCoverageController())
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
    assert "Codified (state-only → config):" in report    # codification guard exercised


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
    empty_state = {"values": {"root_module": {"resources": []}}}
    rc, report = _run(monkeypatch, tmp_path, plan, [], empty_state)
    assert 'resource "unifi_device" "example_ap_2"' not in (tmp_path / "devices.tf").read_text()
    assert "unifi_device.example_ap_2: still referenced at groups.tf:3" in report
    assert rc == 12          # deletion captured + dangler needs attention


def test_staged_deletion_without_references_stays_captured_only(monkeypatch, tmp_path):
    plan = _gone_device_fixture(tmp_path, with_group_ref=False)
    empty_state = {"values": {"root_module": {"resources": []}}}
    rc, report = _run(monkeypatch, tmp_path, plan, [], empty_state)
    assert "still referenced" not in report
    assert rc == 10
