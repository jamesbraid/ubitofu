# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 James Braid
"""Tests for derive_identity — the single source of truth for resource identity.

These tests kill the whole class of reconcile bug where extract_id (controller
side) and _identity (state side) produce different strings for the same logical
object, causing that resource type to be classified as "new" and re-appended on
every run.

RED before fix:  importing derive_identity fails (does not exist yet)
GREEN after fix: all assertions pass because both sides delegate to derive_identity
"""
import io

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from ubitofu.config import Config
from ubitofu.enumerator import (
    ImportTarget,
    derive_identity,  # RED before fix: ImportError
    extract_id,
)
from ubitofu.manifest import MANIFEST, ResourceSpec
from ubitofu.pipeline import _identity

# ---------------------------------------------------------------------------
# 1. Parametrized symmetry test: for every id_rule in the manifest, a
#    representative controller object and the matching state row produce the
#    same identity string via derive_identity, extract_id, and _identity.
#
#    This is the class-catching test: any future asymmetry for any id_rule
#    breaks at least one parametrize case.
# ---------------------------------------------------------------------------

# One (controller_obj, state_row) pair per id_rule.
# Controller objects use _id; state rows use id. Both may have mac / network_id.
_SYMMETRY_CASES: dict[str, tuple[dict, dict, str, str]] = {
    # (controller_obj, state_row, site, expected_identity)
    "_id": (
        {"_id": "abc123", "name": "x"},
        {"id": "abc123", "name": "x"},
        "default",
        "abc123",
    ),
    "mac": (
        {"_id": "abc123", "mac": "aa:bb:cc:dd:ee:ff"},
        {"id": "abc123", "mac": "aa:bb:cc:dd:ee:ff"},
        "default",
        "aa:bb:cc:dd:ee:ff",
    ),
    "mac_or_id_with_mac": (
        {"_id": "abc123", "mac": "aa:bb:cc:dd:ee:ff"},
        {"id": "abc123", "mac": "aa:bb:cc:dd:ee:ff"},
        "default",
        "aa:bb:cc:dd:ee:ff",
    ),
    "mac_or_id_without_mac": (
        {"_id": "abc123"},
        {"id": "abc123"},
        "default",
        "abc123",
    ),
    "site": (
        {},                                      # singleton: no object id on controller
        {"id": "default", "site": "default"},    # provider stores id = site
        "default",
        "default",
    ),
    "site_non_default": (
        {},
        {"id": "mysite", "site": "mysite"},
        "mysite",
        "mysite",
    ),
    "site:_id": (
        {"_id": "obj1"},
        {"id": "obj1"},                          # provider stores bare id, site separate
        "default",
        "default:obj1",
    ),
    "wg_two_level": (
        # enumerator augments peer with network_id before calling derive_identity
        {"_id": "PEER1", "network_id": "NET1", "name": "example_peer"},
        {"id": "PEER1", "network_id": "NET1", "name": "example_peer"},
        "default",
        "NET1:PEER1",
    ),
}

# Build a ResourceSpec stub for each id_rule so we can call extract_id.
_SPEC_FOR: dict[str, ResourceSpec] = {
    "_id":             ResourceSpec("unifi_network", "rest/networkconf", "_id"),
    "mac":             ResourceSpec("unifi_client", "rest/user", "mac"),
    "mac_or_id_with_mac":   ResourceSpec("unifi_device", "stat/device", "mac_or_id"),
    "mac_or_id_without_mac": ResourceSpec("unifi_device", "stat/device", "mac_or_id"),
    "site":            ResourceSpec("unifi_setting", "get/setting", "site"),
    "site_non_default": ResourceSpec("unifi_setting", "get/setting", "site"),
    "site:_id":        ResourceSpec("unifi_hypothetical", "rest/example", "site:_id"),
    "wg_two_level":    ResourceSpec("unifi_wireguard_peer",
                                    "v2/api/site/{site}/wireguard", "wg_two_level"),
}


@pytest.mark.parametrize("case_name,case", list(_SYMMETRY_CASES.items()))
def test_derive_identity_symmetric_controller_and_state(case_name, case):
    """Controller object and state row for the same logical object produce the
    same identity string when both use derive_identity.

    This is the class-catching test: add a new id_rule without updating
    derive_identity and at least one case breaks.
    """
    ctrl_obj, state_row, site, expected = case
    id_rule = _SPEC_FOR[case_name].id_rule

    ctrl_id = derive_identity(id_rule, ctrl_obj, site)
    state_id = derive_identity(id_rule, state_row, site)

    assert ctrl_id == expected, (
        f"controller side wrong for {case_name!r}: "
        f"expected {expected!r}, got {ctrl_id!r}"
    )
    assert state_id == expected, (
        f"state side wrong for {case_name!r}: "
        f"expected {expected!r}, got {state_id!r}"
    )
    assert ctrl_id == state_id, (
        f"ASYMMETRY for {case_name!r}: controller={ctrl_id!r} state={state_id!r}"
    )


@pytest.mark.parametrize("case_name,case", list(_SYMMETRY_CASES.items()))
def test_extract_id_and_identity_agree(case_name, case):
    """extract_id (controller path) and _identity (state path) produce the same
    string as each other and as derive_identity for every id_rule.
    """
    ctrl_obj, state_row, site, expected = case
    spec = _SPEC_FOR[case_name]
    id_rule = spec.id_rule

    # Enumerator/controller side
    ctrl_result = extract_id(ctrl_obj, spec, site)
    # Pipeline/state side  — _identity now delegates to derive_identity
    state_result = _identity(id_rule, state_row, site)

    assert ctrl_result == expected, (
        f"extract_id wrong for {case_name!r}: expected {expected!r}, "
        f"got {ctrl_result!r}"
    )
    assert state_result == expected, (
        f"_identity wrong for {case_name!r}: expected {expected!r}, "
        f"got {state_result!r}"
    )
    assert ctrl_result == state_result, (
        f"extract_id vs _identity mismatch for {case_name!r}: "
        f"{ctrl_result!r} != {state_result!r}"
    )


# ---------------------------------------------------------------------------
# 2. Manifest coverage: every distinct id_rule in the manifest is handled by
#    derive_identity (does not raise), and an unknown rule raises.
# ---------------------------------------------------------------------------

def test_manifest_all_id_rules_covered():
    """Every id_rule appearing in MANIFEST must be handled by derive_identity.

    If a new rule is added to the manifest without a matching branch in
    derive_identity, this test breaks.
    """
    # Minimal records that satisfy each rule
    _minimal_record = {
        "_id":          {"_id": "x"},
        "mac":          {"mac": "aa:bb:cc:dd:ee:ff"},
        "mac_or_id":    {"_id": "x"},
        "site":         {},
        "site:_id":     {"_id": "x"},
        "wg_two_level": {"_id": "p", "network_id": "n"},
    }
    distinct_rules = {s.id_rule for s in MANIFEST}
    for rule in distinct_rules:
        record = _minimal_record.get(rule, {"_id": "fallback"})
        result = derive_identity(rule, record, "default")
        assert result is not None, (
            f"derive_identity returned None for rule {rule!r} with record {record!r}"
        )


def test_every_manifest_id_rule_has_symmetry_case():
    """Every id_rule in manifest.MANIFEST must have at least one entry in
    _SYMMETRY_CASES / _SPEC_FOR so the parametrized symmetry test exercises it.

    A new id_rule added to MANIFEST without a symmetry case would pass
    test_manifest_all_id_rules_covered (derive_identity handles it) but the
    symmetry between controller and state shapes would be untested — the gap
    this test closes (mutation 6 from the adversarial audit).
    """
    manifest_rules = {spec.id_rule for spec in MANIFEST}
    covered_rules = {spec.id_rule for spec in _SPEC_FOR.values()}
    missing = manifest_rules - covered_rules
    assert not missing, (
        f"id_rule(s) in MANIFEST lack a symmetry case in _SYMMETRY_CASES/_SPEC_FOR: "
        f"{sorted(missing)!r} — add a (controller_obj, state_row, site, expected) "
        "entry to _SYMMETRY_CASES and a matching ResourceSpec stub to _SPEC_FOR"
    )


def test_derive_identity_wg_two_level_requires_both_nid_and_pid():
    """wg_two_level composite needs BOTH network_id and object id present.

    The guard is `nid is not None AND pid is not None`; if either is missing the
    result must be None (the caller then skips the malformed peer) rather than a
    half-built "nid:None"/"None:pid" string.
    """
    assert derive_identity("wg_two_level", {"network_id": "n1", "_id": "p1"}, "s") == "n1:p1"
    assert derive_identity("wg_two_level", {"network_id": "n1"}, "s") is None
    assert derive_identity("wg_two_level", {"_id": "p1"}, "s") is None


def test_derive_identity_unknown_rule_raises():
    """An unknown id_rule must raise ValueError, never silently return wrong data.

    Adding a new rule without updating derive_identity must fail loudly so the
    developer knows the symmetry test needs a new case too.
    """
    with pytest.raises(ValueError, match="unknown id_rule"):
        derive_identity("nonexistent_rule", {"_id": "x"}, "default")


# ---------------------------------------------------------------------------
# 3. Hypothesis property: across generated id/mac/network-id/site values,
#    controller and state sides always agree and derivation is deterministic.
# ---------------------------------------------------------------------------

_safe_id = st.text(
    alphabet=st.characters(
        categories=("Ll", "Lu", "Nd"),
        include_characters="_-:",
    ),
    min_size=1,
    max_size=32,
)
_mac_strategy = st.from_regex(r"[0-9a-f]{2}(:[0-9a-f]{2}){5}", fullmatch=True)
_site_strategy = st.from_regex(r"[a-z][a-z0-9_-]{0,19}", fullmatch=True)


@given(oid=_safe_id, mac=_mac_strategy, nid=_safe_id, site=_site_strategy)
@settings(max_examples=200)
def test_derive_identity_controller_state_agree_hypothesis(oid, mac, nid, site):
    """For generated id/mac/network_id/site values, the controller-object record
    and the state-row record for the same logical object produce the same identity
    string for every id_rule.
    """
    cases: list[tuple[str, dict, dict]] = [
        ("_id",
         {"_id": oid},
         {"id": oid}),
        ("mac",
         {"mac": mac, "_id": oid},
         {"mac": mac, "id": oid}),
        ("mac_or_id",
         {"mac": mac, "_id": oid},
         {"mac": mac, "id": oid}),
        ("mac_or_id",
         {"_id": oid},
         {"id": oid}),
        ("site",
         {},
         {"id": site}),
        ("site:_id",
         {"_id": oid},
         {"id": oid}),
        ("wg_two_level",
         {"_id": oid, "network_id": nid},
         {"id": oid, "network_id": nid}),
    ]
    for id_rule, ctrl_obj, state_row in cases:
        ctrl_id = derive_identity(id_rule, ctrl_obj, site)
        state_id = derive_identity(id_rule, state_row, site)
        assert ctrl_id == state_id, (
            f"asymmetry for id_rule={id_rule!r} oid={oid!r} mac={mac!r} "
            f"nid={nid!r} site={site!r}: "
            f"controller={ctrl_id!r} state={state_id!r}"
        )


@given(oid=_safe_id, site=_site_strategy)
@settings(max_examples=100)
def test_derive_identity_is_deterministic(oid, site):
    """derive_identity must be pure: same inputs always produce the same output."""
    for id_rule in ("_id", "mac_or_id", "site", "site:_id"):
        record = {"_id": oid, "id": oid}
        r1 = derive_identity(id_rule, record, site)
        r2 = derive_identity(id_rule, record, site)
        assert r1 == r2


# ---------------------------------------------------------------------------
# 4. Real-fixture regression: managed site singleton (unifi_setting) must NOT
#    be re-appended on a reconcile run.
#
#    Analogue of test_reconcile_managed_wireguard_peer_not_reappended.
#    Before the structural fix, _identity("site", ...) worked accidentally
#    because values.get("id") == site_name by provider convention.
#    After the fix, derive_identity makes both sides structurally explicit.
# ---------------------------------------------------------------------------

_SETTING_SCHEMA = {"provider_schemas": {
    "registry.opentofu.org/ubiquiti-community/unifi": {"resource_schemas": {
        "unifi_setting": {"block": {"attributes": {
            "site": {"type": "string", "optional": True},
        }}},
    }}}}


def test_reconcile_managed_site_singleton_not_reappended(monkeypatch, tmp_path):
    """Regression: a unifi_setting already in state must not be re-appended.

    Bug path (mirrors the wireguard_peer regression):
    - enumerator emits import_id = "default" (site name) for unifi_setting
    - state row has id="default" (provider stores id = site_name)
    - _identity must recognise the match so new_targets classifies it as managed
    - reconciled_new.tf must never be created

    Note: the provider coincidentally stores id = site_name for site singletons,
    so the OLD two-function code happened to work correctly for this specific case.
    The fix makes it structurally explicit via derive_identity.
    """
    import ubitofu.pipeline as pl
    from ubitofu.enumerator import EnumerationResult

    state = {"values": {"root_module": {"resources": [
        {"type": "unifi_setting", "name": "setting",
         "values": {"id": "default", "site": "default"}},
    ]}}}

    # planned_values carries a unifi_setting resource under slug "setting_2"
    # (assign_slugs bumps because "unifi_setting.setting" is in reserved from
    # both the committed file and state).  This mirrors the wireguard-peer test
    # which uses "example_peer_2": it forces the append path to run so that a drifted
    # derive_identity("site", ...) actually causes reconciled_new.tf to be written
    # and the assertion to fail — making the test load-bearing.
    plan = {
        "resource_changes": [],
        "planned_values": {"root_module": {"resources": [
            {"type": "unifi_setting", "name": "setting_2",
             "values": {"site": "default"}},
        ]}},
    }

    targets = [ImportTarget("unifi_setting", "setting", "default")]

    class SettingRunner:
        def __init__(self, workdir: object) -> None:
            self.workdir = workdir

        def plan(self, *, out: object = None, generate_config_out: object = None) -> int:
            if generate_config_out is not None:
                import pathlib
                pathlib.Path(str(generate_config_out)).write_text("# stub\n")
            return 0

        def providers_schema(self) -> dict:
            return _SETTING_SCHEMA

        def show_json(self, plan_file: object) -> dict:
            return plan

        def show_state_json(self) -> dict:
            return state

    # Write a minimal committed file so reconcile has something to scan.
    (tmp_path / "setting.tf").write_text(
        'resource "unifi_setting" "setting" {\n'
        '  site = "default"\n'
        '}\n'
    )

    class FakeCoverageController:
        site = "default"

        def collection(self, endpoint):
            return []

    monkeypatch.setattr(pl, "Controller", lambda **kw: FakeCoverageController())
    monkeypatch.setattr(pl, "enumerate_controller",
                        lambda ctl: EnumerationResult(targets=targets, gaps=[]))
    monkeypatch.setattr(pl, "TofuRunner",
                        lambda workdir: SettingRunner(workdir))
    monkeypatch.setenv("UNIFI_API_KEY", "k")
    cfg = Config("https://unifi.example", "default", "env", "UNIFI_API_KEY",
                 "ExampleVault", workdir=str(tmp_path))
    out = io.StringIO()
    rc = pl.run_reconcile(cfg, out)

    assert rc == 0
    new_tf = tmp_path / "reconciled_new.tf"
    assert not new_tf.exists(), (
        "managed unifi_setting wrongly re-appended — "
        f"reconciled_new.tf content:\n{new_tf.read_text()}"
    )
