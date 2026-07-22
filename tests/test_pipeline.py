# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 James Braid
"""Mutation-hardening tests for pipeline.run_generate."""
import io
from pathlib import Path

import ubitofu.pipeline as pl
from ubitofu.config import Config
from ubitofu.enumerator import EnumerationResult, ImportTarget

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

# Carries a bare unifi_setting so the coverage audit's setting_schema_sections
# has a resource to read (it refuses to run without one — no blind audit). Build
# never dereferences it: _EMPTY_PLANNED has no resources to look up.
_EMPTY_SCHEMA = {"provider_schemas": {"registry.terraform.io/jamesbraid/unifi": {
    "resource_schemas": {"unifi_setting": {"block": {"attributes": {}}}}}}}
_EMPTY_PLANNED = {"planned_values": {"root_module": {"resources": []}}}


class _FakeController:
    """Minimal controller for run_generate tests. The coverage audit wired into
    run_generate calls ctl.collection() over get/setting + probe endpoints; an
    empty return means no coverage findings, leaving each test's real assertions
    (targets, plan args, output) untouched."""

    site = "default"

    def collection(self, endpoint):  # noqa: ARG002 — endpoint ignored by design
        return []
_EMPTY_STATE: dict = {"values": {"root_module": {"resources": []}}}
_MANAGED_STATE = {
    "values": {"root_module": {"resources": [{
        "type": "unifi_network",
        "name": "managed_net",
        "values": {"id": "net001"},
    }]}}
}


def _cfg(tmp_path: Path) -> Config:
    return Config(
        "https://unifi.example", "default", "env", "UNIFI_API_KEY",
        "TestVault", workdir=str(tmp_path),
    )


class _FakeRunner:
    """Configurable fake TofuRunner for run_generate tests.

    *create_stub*: plan() writes a stub file to generate_config_out (mimics
    real tofu generating a scaffold).

    *raises_if_stub_exists*: plan() errors if generate_config_out already exists
    at call time — lets tests verify the pre-plan unlink actually ran.

    Call arguments are recorded on plan_out, plan_gco, and show_json_arg.
    """

    def __init__(
        self,
        workdir,
        *,
        create_stub: bool = False,
        raises_if_stub_exists: bool = False,
        state: dict | None = None,
    ):
        self.workdir = workdir
        self._create_stub = create_stub
        self._raises_if_stub_exists = raises_if_stub_exists
        self._state = state if state is not None else _EMPTY_STATE
        # Recorded call args
        self.plan_out: object = None
        self.plan_gco: object = None
        self.show_json_arg: object = None

    def plan(self, *, out=None, generate_config_out=None):
        self.plan_out = out
        self.plan_gco = generate_config_out
        if self._raises_if_stub_exists and generate_config_out is not None:
            if Path(generate_config_out).exists():
                raise FileExistsError(
                    f"run_generate must unlink {generate_config_out} before plan"
                )
        if self._create_stub and generate_config_out is not None:
            Path(generate_config_out).write_text("# stub\n")
        return 0

    def providers_schema(self):
        return _EMPTY_SCHEMA

    def show_json(self, plan_file):
        self.show_json_arg = plan_file
        return _EMPTY_PLANNED

    def show_state_json(self):
        return self._state

    def is_clean(self, code: int) -> bool:
        return code == 0


def _run(
    monkeypatch,
    tmp_path: Path,
    mode: str = "bulk",
    *,
    runner: _FakeRunner | None = None,
    enumerate_result: EnumerationResult | None = None,
    controller_factory=None,
) -> tuple[int, str]:
    """Minimal run_generate harness; returns (rc, out_text)."""
    if runner is None:
        runner = _FakeRunner(tmp_path)

    monkeypatch.setattr(pl, "TofuRunner", lambda workdir: runner)
    if controller_factory is not None:
        monkeypatch.setattr(pl, "controller_from_config", controller_factory)
    else:
        monkeypatch.setattr(pl, "controller_from_config", lambda cfg: _FakeController())
    result_er = enumerate_result or EnumerationResult(targets=[], gaps=[])
    monkeypatch.setattr(pl, "enumerate_controller", lambda ctl: result_er)
    monkeypatch.setenv("UNIFI_API_KEY", "test-api-key")

    out = io.StringIO()
    rc = pl.run_generate(_cfg(tmp_path), mode, out)
    return rc, out.getvalue()


# ---------------------------------------------------------------------------
# Controller construction: all kwargs must come from cfg  (mutmut_1-7, 10)
# ---------------------------------------------------------------------------

def test_run_generate_controller_args_come_from_cfg(monkeypatch, tmp_path):
    """The controller is built by controller_from_config(cfg) — cfg is the
    single source of truth for controller_url/site/dialect/credentials
    (Task 6's factory owns kwarg assembly, tested directly in
    test_controller.py) — and the resulting instance is passed to
    enumerate_controller."""
    captured: dict = {}
    sentinel_ctl = _FakeController()

    def fake_controller_from_config(cfg):
        captured["cfg"] = cfg
        return sentinel_ctl

    def fake_enumerate(ctl):
        captured["ctl"] = ctl
        return EnumerationResult()

    monkeypatch.setattr(pl, "controller_from_config", fake_controller_from_config)
    monkeypatch.setattr(pl, "enumerate_controller", fake_enumerate)
    monkeypatch.setattr(pl, "TofuRunner", lambda workdir: _FakeRunner(workdir))
    monkeypatch.setenv("UNIFI_API_KEY", "my-secret-key")

    cfg = Config(
        "https://unifi.example", "homelab", "env", "UNIFI_API_KEY",
        "TestVault", workdir=str(tmp_path),
    )
    out = io.StringIO()
    pl.run_generate(cfg, "bulk", out)

    assert captured["cfg"] is cfg
    # The object returned by controller_from_config() must reach
    # enumerate_controller (not None)
    assert captured["ctl"] is sentinel_ctl


# ---------------------------------------------------------------------------
# TofuRunner workdir arg  (mutmut_14)
# ---------------------------------------------------------------------------

def test_run_generate_workdir_passed_to_runner(monkeypatch, tmp_path):
    """TofuRunner must receive workdir=Path(cfg.workdir), not None."""
    captured_workdir: dict = {}

    def fake_runner_cls(workdir):
        captured_workdir["workdir"] = workdir
        return _FakeRunner(workdir)

    monkeypatch.setattr(pl, "TofuRunner", fake_runner_cls)
    monkeypatch.setattr(pl, "controller_from_config", lambda cfg: _FakeController())
    monkeypatch.setattr(pl, "enumerate_controller", lambda ctl: EnumerationResult())
    monkeypatch.setenv("UNIFI_API_KEY", "k")

    pl.run_generate(_cfg(tmp_path), "bulk", io.StringIO())
    assert captured_workdir["workdir"] == Path(str(tmp_path))


# ---------------------------------------------------------------------------
# Incremental mode: already-managed targets must be filtered out  (mutmut_17, 18)
# ---------------------------------------------------------------------------

def test_run_generate_incremental_filters_managed_targets(monkeypatch, tmp_path):
    """In incremental mode only targets NOT yet in state are imported."""
    # State has net001 managed; targets include both managed and a new one.
    runner = _FakeRunner(tmp_path, state=_MANAGED_STATE)
    targets = [
        ImportTarget("unifi_network", "managed_net", "net001"),  # already managed
        ImportTarget("unifi_network", "new_net", "net999"),       # NEW
    ]
    _run(
        monkeypatch, tmp_path, mode="incremental",
        runner=runner,
        enumerate_result=EnumerationResult(targets=targets, gaps=[]),
    )
    imports_text = (tmp_path / "imports.tf").read_text()
    assert "net999" in imports_text
    assert "net001" not in imports_text


def test_run_generate_incremental_site_passed_to_state_identities(monkeypatch, tmp_path):
    """cfg.site must be forwarded to state_identities so site-singleton resources
    (id_rule='site') are recognised as managed and filtered out, not re-imported.

    unifi_setting uses id_rule='site': its identity IS the site name.
    With site=None or site='' the identity never matches 'default' and the
    managed target leaks through to imports.tf.
    """
    state = {"values": {"root_module": {"resources": [{
        "type": "unifi_setting",
        "name": "setting",
        "values": {},  # identity derived from site arg, not from values
    }]}}}
    runner = _FakeRunner(tmp_path, state=state)
    # This target's import_id == the site name; it should be filtered as managed.
    targets = [ImportTarget("unifi_setting", "setting", "default")]
    _run(
        monkeypatch, tmp_path, mode="incremental",
        runner=runner,
        enumerate_result=EnumerationResult(targets=targets, gaps=[]),
    )
    imports_text = (tmp_path / "imports.tf").read_text()
    # managed singleton must NOT appear in imports
    assert '"default"' not in imports_text


# ---------------------------------------------------------------------------
# Output file paths  (mutmut_22, 26, 27 / 31, 36 in renumbered run)
# Note: use iterdir() name-set to get the stored filename, not just existence —
# macOS HFS+ is case-insensitive so exists() returns True for "GENERATED.TF"
# even when the required name is "generated.tf".
# ---------------------------------------------------------------------------

def test_run_generate_bulk_creates_generated_tf(monkeypatch, tmp_path):
    """Bulk mode writes HCL to 'generated.tf' (exact lower-case name)."""
    _run(monkeypatch, tmp_path, mode="bulk")
    tf_names = {f.name for f in tmp_path.iterdir()}
    assert "generated.tf" in tf_names


def test_run_generate_incremental_creates_generated_new_tf(monkeypatch, tmp_path):
    """Incremental mode writes HCL to 'generated_new.tf' (exact lower-case name)."""
    _run(monkeypatch, tmp_path, mode="incremental")
    tf_names = {f.name for f in tmp_path.iterdir()}
    assert "generated_new.tf" in tf_names
    assert "generated.tf" not in tf_names


# ---------------------------------------------------------------------------
# imports.tf written with correct filename  (mutmut_30, 31 / 40 in renumbered run)
# ---------------------------------------------------------------------------

def test_run_generate_writes_imports_tf(monkeypatch, tmp_path):
    """Import blocks must be written to 'imports.tf' (exact lower-case name)."""
    targets = [ImportTarget("unifi_network", "examplenet", "net001")]
    _run(
        monkeypatch, tmp_path,
        enumerate_result=EnumerationResult(targets=targets, gaps=[]),
    )
    tf_names = {f.name for f in tmp_path.iterdir()}
    assert "imports.tf" in tf_names
    text = next(f for f in tmp_path.iterdir() if f.name == "imports.tf").read_text()
    assert "net001" in text


# ---------------------------------------------------------------------------
# Pre-plan unlink of generated_stub.tf  (mutmut_35, 36)
# ---------------------------------------------------------------------------

def test_run_generate_unlinks_stub_before_plan(monkeypatch, tmp_path):
    """A pre-existing generated_stub.tf must be deleted before runner.plan() runs.

    If the first unlink is a no-op (wrong filename), plan() finds the old stub
    and raises FileExistsError — verifying the unlink path is correct.
    """
    (tmp_path / "generated_stub.tf").write_text("# leftover stub\n")
    runner = _FakeRunner(tmp_path, raises_if_stub_exists=True)
    rc, _ = _run(monkeypatch, tmp_path, runner=runner)
    assert rc == 0


def test_run_generate_unlinks_stub_by_exact_name(monkeypatch, tmp_path):
    """The stub is unlinked twice (pre-plan and post-plan) by its exact name.

    Assert the literal names passed to Path.unlink rather than checking file
    existence: a case/name mutation of "generated_stub.tf" is invisible to an
    existence check on a case-insensitive filesystem (macOS) but is a real,
    killable defect on the case-sensitive Linux CI host. Spying on the call
    catches it on any platform.
    """
    unlinked: list[str] = []
    real_unlink = Path.unlink

    def spy_unlink(self: Path, *args, **kwargs):
        unlinked.append(self.name)
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", spy_unlink)
    _run(monkeypatch, tmp_path, mode="bulk")
    assert unlinked.count("generated_stub.tf") == 2


# ---------------------------------------------------------------------------
# runner.plan() call arguments  (mutmut_40-43, 45, 46, 48, 49)
# ---------------------------------------------------------------------------

def test_run_generate_plan_called_with_correct_args(monkeypatch, tmp_path):
    """plan() must be called with out=workdir/tf.plan and
    generate_config_out=workdir/generated_stub.tf (exact paths)."""
    runner = _FakeRunner(tmp_path)
    _run(monkeypatch, tmp_path, runner=runner)
    workdir = Path(str(tmp_path))
    assert runner.plan_out == workdir / "tf.plan"
    assert runner.plan_gco == workdir / "generated_stub.tf"


# ---------------------------------------------------------------------------
# runner.show_json() argument  (mutmut_52, 54, 55)
# ---------------------------------------------------------------------------

def test_run_generate_show_json_called_with_tf_plan(monkeypatch, tmp_path):
    """show_json() must be called with workdir/tf.plan (not None, not a typo)."""
    runner = _FakeRunner(tmp_path)
    _run(monkeypatch, tmp_path, runner=runner)
    assert runner.show_json_arg == Path(str(tmp_path)) / "tf.plan"


# ---------------------------------------------------------------------------
# write_variables_tf merge flag  (mutmut_66, 70, 71, 72)
# ---------------------------------------------------------------------------

def test_run_generate_bulk_does_not_merge_variables(monkeypatch, tmp_path):
    """Bulk mode must overwrite unifi-variables.tf; stale declarations must vanish."""
    vf = tmp_path / "unifi-variables.tf"
    vf.write_text(
        'variable "stale_secret" {\n  type      = string\n  sensitive = true\n}\n'
    )
    _run(monkeypatch, tmp_path, mode="bulk")
    assert "stale_secret" not in vf.read_text()


def test_run_generate_incremental_merges_variables(monkeypatch, tmp_path):
    """Incremental mode must merge into unifi-variables.tf, not overwrite it."""
    vf = tmp_path / "unifi-variables.tf"
    vf.write_text(
        'variable "existing_psk" {\n  type      = string\n  sensitive = true\n}\n'
    )
    _run(monkeypatch, tmp_path, mode="incremental")
    # Pre-existing declaration must survive the merge.
    assert "existing_psk" in vf.read_text()


# ---------------------------------------------------------------------------
# Post-plan unlink of generated_stub.tf — filename  (mutmut_75, 76)
# ---------------------------------------------------------------------------

def test_run_generate_stub_cleaned_after_plan(monkeypatch, tmp_path):
    """generated_stub.tf must not exist after run_generate returns.

    plan() creates the stub (like real tofu would); the second unlink must
    delete it under its exact name.
    """
    runner = _FakeRunner(tmp_path, create_stub=True)
    rc, _ = _run(monkeypatch, tmp_path, runner=runner)
    assert rc == 0
    assert not (tmp_path / "generated_stub.tf").exists()


# ---------------------------------------------------------------------------
# Post-plan unlink missing_ok  (mutmut_73, 77)
# ---------------------------------------------------------------------------

def test_run_generate_stub_second_unlink_missing_ok(monkeypatch, tmp_path):
    """The second unlink of generated_stub.tf must use missing_ok=True.

    When plan() does not create the stub (no generate_config_out written),
    the second unlink would raise FileNotFoundError if missing_ok is falsy.
    """
    # FakeRunner does NOT create the stub — second unlink must tolerate absence.
    runner = _FakeRunner(tmp_path, create_stub=False)
    rc, _ = _run(monkeypatch, tmp_path, runner=runner)
    assert rc == 0


# ---------------------------------------------------------------------------
# format_gaps printed to out  (mutmut_78, 79, 80, 81, 82)
# ---------------------------------------------------------------------------

def test_run_generate_prints_gaps_to_out(monkeypatch, tmp_path):
    """format_gaps(res.gaps) must be printed to the out stream, not stdout.

    With non-empty gaps the output contains 'Coverage gaps' and the gap text;
    mutations that pass None, drop the arg, or redirect output all fail.
    """
    gaps = ["unifi_firewall_zone — 1 object skipped"]
    rc, text = _run(
        monkeypatch, tmp_path,
        enumerate_result=EnumerationResult(targets=[], gaps=gaps),
    )
    assert rc == 0
    assert "Coverage gaps" in text
    assert "unifi_firewall_zone" in text


# ---------------------------------------------------------------------------
# op_refs printed when non-empty  (smoke coverage — kills mutmut_79 for this path)
# ---------------------------------------------------------------------------

# The op_refs print path is already covered by the existing
# test_run_generate_emits_variables_and_prints_op_refs in test_integration.py.


# ---------------------------------------------------------------------------
# Incremental footer message  (mutmut_88, 89, 90)
# ---------------------------------------------------------------------------

def test_run_generate_incremental_prints_count_message(monkeypatch, tmp_path):
    """Incremental mode must print 'Incremental: N new object(s)' to out."""
    targets = [
        ImportTarget("unifi_network", "net_a", "net001"),
        ImportTarget("unifi_network", "net_b", "net002"),
    ]
    _, text = _run(
        monkeypatch, tmp_path, mode="incremental",
        enumerate_result=EnumerationResult(targets=targets, gaps=[]),
    )
    assert "Incremental" in text
    assert "2 new object(s)" in text


def test_run_generate_bulk_no_incremental_message(monkeypatch, tmp_path):
    """Bulk mode must NOT print the 'Incremental:' footer."""
    targets = [ImportTarget("unifi_network", "net_a", "net001")]
    _, text = _run(
        monkeypatch, tmp_path, mode="bulk",
        enumerate_result=EnumerationResult(targets=targets, gaps=[]),
    )
    assert "Incremental" not in text


# ---------------------------------------------------------------------------
# classify_diverged: committed-config resources whose plan diverged. A plan
# `create` alone cannot distinguish "merged but not yet applied" from "object
# deleted on the controller" (both have before=None) — the live enumeration
# disambiguates. Devices removed in the UI kept being reported
# as "run apply", which cannot work (device adoption is UI-only).
# ---------------------------------------------------------------------------

_LIVE = {"unifi_device": {"aa:bb:cc:dd:ee:ff"}}


def test_classify_create_present_live_is_pending():
    change = {"actions": ["create"], "before": None,
              "after": {"mac": "aa:bb:cc:dd:ee:ff", "name": "example AP"}}
    assert pl.classify_diverged("unifi_device", change, _LIVE) == "pending"


def test_classify_create_gone_from_controller_is_deleted():
    change = {"actions": ["create"], "before": None,
              "after": {"mac": "11:22:33:44:55:66", "name": "example AP 2"}}
    assert pl.classify_diverged("unifi_device", change, _LIVE) == "deleted"


def test_classify_create_underivable_identity_stays_pending():
    # id_rule "_id" resources have no derivable identity before first apply;
    # absence cannot be proven, so keep the conservative tag.
    change = {"actions": ["create"], "before": None, "after": {"name": "x"}}
    assert pl.classify_diverged(
        "unifi_network", change, {"unifi_network": {"net001"}}) == "pending"


def test_classify_create_state_identity_gone_is_deleted():
    # Previously-applied resource: identity comes from the state row even when
    # the committed values carry none (id is computed for _id-ruled types).
    change = {"actions": ["create"], "before": None, "after": {"name": "oldnet"}}
    assert pl.classify_diverged(
        "unifi_network", change, {"unifi_network": {"net001"}},
        state_identity="net066") == "deleted"


def test_classify_create_state_identity_present_is_pending():
    change = {"actions": ["create"], "before": None, "after": {"name": "n"}}
    assert pl.classify_diverged(
        "unifi_network", change, {"unifi_network": {"net001"}},
        state_identity="net001") == "pending"


def test_classify_delete_is_deleted():
    change = {"actions": ["delete"],
              "before": {"mac": "aa:bb:cc:dd:ee:ff"}, "after": None}
    assert pl.classify_diverged("unifi_device", change, _LIVE) == "deleted"


def test_classify_replace_is_diverged():
    change = {"actions": ["delete", "create"],
              "before": {"mac": "aa:bb:cc:dd:ee:ff"},
              "after": {"mac": "aa:bb:cc:dd:ee:ff"}}
    assert pl.classify_diverged("unifi_device", change, _LIVE) == "diverged"


def test_classify_unknown_type_stays_pending():
    # No manifest spec -> no id_rule -> absence cannot be proven.
    change = {"actions": ["create"], "before": None, "after": {"mac": "aa"}}
    assert pl.classify_diverged("unifi_mystery", change, {}) == "pending"


def test_classify_creatable_new_object_absent_live_is_pending():
    # A hand-authored NEW unifi_client (mac derivable, absent live, never
    # applied) is pending create intent — apply will create it. v0.5.0
    # mislabeled this cell "deleted".
    change = {"actions": ["create"], "before": None,
              "after": {"mac": "00:11:22:00:00:03", "name": "client_c"}}
    assert pl.classify_diverged("unifi_client", change, {"unifi_client": set()}) == "pending"


def test_classify_ui_lifecycle_absent_live_is_deleted_even_unapplied():
    # A device block whose MAC is not on the controller can never be
    # created by tofu — deleted regardless of state history.
    change = {"actions": ["create"], "before": None,
              "after": {"mac": "11:22:33:44:55:66", "name": "example AP 2"}}
    assert pl.classify_diverged("unifi_device", change, {"unifi_device": set()}) == "deleted"


def test_classify_creatable_applied_then_gone_is_deleted():
    # Was applied (state identity known), object gone live: deleted for any type.
    change = {"actions": ["create"], "before": None, "after": {"name": "oldnet"}}
    assert pl.classify_diverged(
        "unifi_network", change, {"unifi_network": {"net001"}},
        state_identity="net066") == "deleted"
