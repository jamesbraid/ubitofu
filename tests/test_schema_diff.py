# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 James Braid
"""Provider-version migration gate: what a bump breaks, read off the schema.

The motivating pair, both from ubiquiti-community/unifi 0.101.0:
  - unifi_device.radio_table.assisted_roaming_enabled was REMOVED, and a
    config that still sets it fails to plan — so there is no plan JSON for
    reconcile to work from. Only a schema diff catches that.
  - unifi_wlan.roaming_assistant_na_enabled was ADDED with a static default
    that overrode the live value. The schema JSON does not carry defaults
    (OpenTofu's jsonprovider serializer emits no such field), so the gate can
    only flag the attribute as new and hand off to a plan.
"""
from ubitofu.schema_diff import (
    attr_locations,
    declares_type,
    diff_resources,
    filter_to_config,
    lock_versions,
    reduce_schema,
)


def _schema(attrs, rtype="unifi_device"):
    return {"provider_schemas": {"registry.terraform.io/x/unifi": {
        "resource_schemas": {rtype: {"block": {"attributes": attrs}}}}}}


# ---------------------------------------------------------------------------
# reduce_schema: the small, diffable projection of `tofu providers schema`.
# ---------------------------------------------------------------------------

def test_reduce_records_the_flags_the_diff_needs():
    out = reduce_schema(_schema({
        "name": {"type": "string", "required": True},
        "vlan": {"type": "number", "optional": True, "computed": True},
    }))
    assert out["unifi_device"]["name"] == {
        "required": True, "optional": False, "computed": False, "deprecated": False}
    assert out["unifi_device"]["vlan"] == {
        "required": False, "optional": True, "computed": True, "deprecated": False}


def test_reduce_recurses_into_nested_attributes():
    """The motivating removal is nested: radio_table[].assisted_roaming_enabled.
    A top-level-only walk would miss the very case this exists for."""
    out = reduce_schema(_schema({
        "radio_table": {"optional": True, "nested_type": {
            "nesting_mode": "list",
            "attributes": {
                "radio": {"type": "string", "optional": True},
                "assisted_roaming_enabled": {"type": "bool", "optional": True},
            }}},
    }))
    assert "radio_table.assisted_roaming_enabled" in out["unifi_device"]
    assert "radio_table.radio" in out["unifi_device"]
    assert "radio_table" in out["unifi_device"]


def test_reduce_recurses_into_block_types():
    schema = {"provider_schemas": {"p": {"resource_schemas": {"unifi_x": {"block": {
        "attributes": {"name": {"type": "string", "required": True}},
        "block_types": {"port_override": {"block": {
            "attributes": {"forward": {"type": "string", "optional": True}}}}},
    }}}}}}
    out = reduce_schema(schema)
    assert "port_override.forward" in out["unifi_x"]
    assert out["unifi_x"]["port_override"]["optional"] is True


def test_reduce_reads_every_provider_in_the_schema():
    schema = {"provider_schemas": {
        "a": {"resource_schemas": {"unifi_x": {"block": {"attributes": {}}}}},
        "b": {"resource_schemas": {"other_y": {"block": {"attributes": {}}}}},
    }}
    assert set(reduce_schema(schema)) == {"unifi_x", "other_y"}


# ---------------------------------------------------------------------------
# diff_resources
# ---------------------------------------------------------------------------

_FLAGS = {"required": False, "optional": True, "computed": False, "deprecated": False}


def _res(**attrs):
    return {"unifi_device": {k: {**_FLAGS, **v} for k, v in attrs.items()}}


def test_removed_attr_is_reported():
    findings = diff_resources(
        _res(name={}, assisted_roaming_enabled={}), _res(name={}))
    kinds = {(f.kind, f.identifier) for f in findings}
    assert ("removed-attr", "unifi_device.assisted_roaming_enabled") in kinds


def test_new_attr_is_reported_with_the_defaults_caveat():
    findings = diff_resources(_res(name={}), _res(name={}, roaming_assistant={}))
    (f,) = [f for f in findings if f.kind == "new-attr"]
    assert f.identifier == "unifi_device.roaming_assistant"
    assert "default" in f.detail


def test_new_required_attr_is_its_own_kind():
    findings = diff_resources(
        _res(name={}), _res(name={}, zone_id={"required": True, "optional": False}))
    assert [f.kind for f in findings] == ["new-required-attr"]


def test_optional_becoming_required_is_reported():
    findings = diff_resources(
        _res(name={}), _res(name={"required": True, "optional": False}))
    assert [f.kind for f in findings] == ["optional-to-required"]


def test_attr_that_stops_being_settable_is_reported():
    """optional -> computed-only breaks a config that still assigns it: tofu
    refuses with "Can't configure a value for X". Same class as a removal, and
    the flags to see it are already in the baseline."""
    findings = diff_resources(
        _res(a={}), _res(a={"optional": False, "computed": True}))
    (f,) = findings
    assert f.kind == "no-longer-settable"
    assert f.identifier == "unifi_device.a"
    assert f.detail.startswith("no longer settable")


def test_required_attr_that_stops_being_settable_is_reported():
    findings = diff_resources(
        _res(a={"required": True, "optional": False}),
        _res(a={"required": False, "optional": False, "computed": True}))
    assert [f.kind for f in findings] == ["no-longer-settable"]


def test_an_attr_that_merely_gains_computed_is_not_flagged():
    # Optional+Computed is the ordinary shape for a controller-managed value
    # the operator may also set. Still settable, so nothing breaks.
    assert diff_resources(_res(a={}), _res(a={"computed": True})) == []


def test_a_newly_settable_attr_is_not_flagged():
    # computed-only -> optional is additive; no config can break on it.
    findings = diff_resources(
        _res(a={"optional": False, "computed": True}), _res(a={}))
    assert findings == []


def test_no_longer_settable_is_located_like_a_removal():
    findings = diff_resources(
        _res(name={}, **{"radio_table.assisted_roaming_enabled": {}}),
        _res(name={}, **{"radio_table.assisted_roaming_enabled":
                         {"optional": False, "computed": True}}))
    (f,) = filter_to_config(findings, {"devices.tf": _DEVICES_TF})
    assert f.kind == "no-longer-settable"
    assert "devices.tf:6" in f.detail


def test_no_longer_settable_the_config_never_sets_is_dropped():
    findings = diff_resources(
        _res(name={}, mesh_sta_vap_enabled={}),
        _res(name={}, mesh_sta_vap_enabled={"optional": False, "computed": True}))
    assert filter_to_config(findings, {"devices.tf": _DEVICES_TF}) == []


def test_newly_deprecated_attr_is_reported():
    findings = diff_resources(_res(name={}), _res(name={"deprecated": True}))
    assert [f.kind for f in findings] == ["deprecated-attr"]


def test_already_deprecated_attr_is_not_reported_again():
    findings = diff_resources(
        _res(name={"deprecated": True}), _res(name={"deprecated": True}))
    assert findings == []


def test_removed_resource_type_is_reported():
    findings = diff_resources(_res(name={}), {})
    assert [(f.kind, f.identifier) for f in findings] == [
        ("removed-resource", "unifi_device")]


def test_new_resource_type_is_left_to_the_coverage_audit():
    # The manifest-lag audit already reports provider resources ubitofu does
    # not map; duplicating it here would double-report every bump.
    assert diff_resources({}, _res(name={})) == []


def test_identical_schemas_diff_clean():
    assert diff_resources(_res(name={}, vlan={}), _res(name={}, vlan={})) == []


# ---------------------------------------------------------------------------
# Config-relevance filter: a finding the committed HCL cannot hit is noise.
# ---------------------------------------------------------------------------

_DEVICES_TF = '''resource "unifi_device" "sw" {
  name = "sw"

  radio_table = [{
    radio                    = "na"
    assisted_roaming_enabled = false
  }]
}
'''


def test_attr_locations_reports_file_and_line():
    assert attr_locations({"devices.tf": _DEVICES_TF}, "unifi_device",
                          "assisted_roaming_enabled") == ["devices.tf:6"]


def test_attr_locations_ignores_a_substring_match():
    text = 'resource "unifi_device" "sw" {\n  assisted_roaming_enabled_x = false\n}\n'
    assert attr_locations({"d.tf": text}, "unifi_device",
                          "assisted_roaming_enabled") == []


def test_attr_locations_ignores_the_name_used_as_a_value():
    text = 'resource "unifi_device" "sw" {\n  note = assisted_roaming_enabled\n}\n'
    assert attr_locations({"d.tf": text}, "unifi_device",
                          "assisted_roaming_enabled") == []


def test_attr_locations_counts_lines_from_the_top_of_the_file():
    # A leading blank line shifts every line number; the count must start at
    # the first byte of the file, not the second.
    text = "\n" + _DEVICES_TF
    assert attr_locations({"d.tf": text}, "unifi_device",
                          "assisted_roaming_enabled") == ["d.tf:7"]


def test_attr_locations_ignores_another_resource_type():
    # The removed attribute belongs to unifi_device. A same-named attribute on
    # an unrelated resource is a different attribute and must not be reported,
    # or the bump gets a blocker naming a line that needs no change.
    text = ('resource "unifi_wlan" "w" {\n  assisted_roaming_enabled = false\n}\n')
    assert attr_locations({"d.tf": text}, "unifi_device",
                          "assisted_roaming_enabled") == []


def test_attr_locations_finds_every_block_of_the_type():
    text = (_DEVICES_TF
            + 'resource "unifi_wlan" "w" {\n  assisted_roaming_enabled = false\n}\n'
            + _DEVICES_TF.replace('"sw"', '"sw2"'))
    assert attr_locations({"d.tf": text}, "unifi_device",
                          "assisted_roaming_enabled") == ["d.tf:6", "d.tf:17"]


def test_a_removal_only_another_resource_assigns_is_dropped():
    findings = diff_resources(_res(name={}, assisted_roaming_enabled={}), _res(name={}))
    texts = {"d.tf": _DEVICES_TF.replace("assisted_roaming_enabled", "other")
             + 'resource "unifi_wlan" "w" {\n  assisted_roaming_enabled = false\n}\n'}
    assert filter_to_config(findings, texts) == []


def test_declares_type_matches_a_resource_block():
    assert declares_type({"d.tf": _DEVICES_TF}, "unifi_device") is True
    assert declares_type({"d.tf": _DEVICES_TF}, "unifi_wlan") is False


def test_removed_attr_the_config_sets_is_kept_and_located():
    findings = diff_resources(
        _res(name={}, **{"radio_table.assisted_roaming_enabled": {}}), _res(name={}))
    kept = filter_to_config(findings, {"devices.tf": _DEVICES_TF})
    (f,) = kept
    assert f.kind == "removed-attr"
    assert "devices.tf:6" in f.detail


def test_removed_attr_the_config_never_sets_is_dropped():
    findings = diff_resources(_res(name={}, mesh_sta_vap_enabled={}), _res(name={}))
    assert filter_to_config(findings, {"devices.tf": _DEVICES_TF}) == []


def test_findings_for_an_unused_resource_type_are_dropped():
    findings = diff_resources(
        {"unifi_wlan": {"a": _FLAGS}}, {"unifi_wlan": {"a": {**_FLAGS, "deprecated": True}}})
    assert filter_to_config(findings, {"devices.tf": _DEVICES_TF}) == []


def test_removed_resource_type_the_config_uses_is_kept():
    findings = diff_resources(_res(name={}), {})
    assert len(filter_to_config(findings, {"devices.tf": _DEVICES_TF})) == 1


# ---------------------------------------------------------------------------
# lock_versions: the bump is detected from what tofu actually installed.
# ---------------------------------------------------------------------------

_LOCK = '''# This file is maintained automatically by "tofu init".
# Manual edits may be lost in future updates.

provider "registry.terraform.io/jamesbraid/unifi" {
  version     = "0.101.1"
  constraints = "0.101.1"
  hashes = [
    "h1:1BQSiu7uZJKRGZoQFURbQzgcAJemM0fsNGm1zbWsGVw=",
  ]
}

provider "registry.opentofu.org/hashicorp/random" {
  version = "3.6.0"
}
'''


def test_lock_versions_reads_every_provider():
    assert lock_versions(_LOCK) == {
        "registry.terraform.io/jamesbraid/unifi": "0.101.1",
        "registry.opentofu.org/hashicorp/random": "3.6.0",
    }


def test_lock_versions_takes_version_not_constraints():
    text = ('provider "p/q" {\n  version     = "1.0.0"\n'
            '  constraints = "9.9.9"\n}\n')
    assert lock_versions(text) == {"p/q": "1.0.0"}


def test_lock_versions_of_an_empty_lock_is_empty():
    assert lock_versions("# nothing here\n") == {}


# ---------------------------------------------------------------------------
# Contract edges the mutation gate probes: omitted maps, exact flag keys, exact
# detail wording, and loop control that must skip one finding without dropping
# the rest.
# ---------------------------------------------------------------------------

def test_reduce_tolerates_every_omitted_map():
    # Terraform's schema JSON omits empty maps, so a resource with no
    # attributes, a nested_type with no children and a provider with no
    # resources all arrive as missing keys rather than empty dicts.
    assert reduce_schema({}) == {}
    assert reduce_schema({"provider_schemas": {"p": {}}}) == {}
    assert reduce_schema({"provider_schemas": {"p": {"resource_schemas": {
        "unifi_x": {}}}}}) == {"unifi_x": {}}
    childless = _schema({"empty": {"optional": True,
                                   "nested_type": {"nesting_mode": "single"}}})
    assert reduce_schema(childless) == {
        "unifi_device": {"empty": {"required": False, "optional": True,
                                   "computed": False, "deprecated": False}}}
    bare_block = {"provider_schemas": {"p": {"resource_schemas": {"unifi_x": {"block": {
        "block_types": {"port_override": {}}}}}}}}
    assert set(reduce_schema(bare_block)["unifi_x"]) == {"port_override"}


def test_block_type_flags_are_writable_and_not_computed():
    schema = {"provider_schemas": {"p": {"resource_schemas": {"unifi_x": {"block": {
        "block_types": {"port_override": {"block": {}}}}}}}}}
    assert reduce_schema(schema)["unifi_x"]["port_override"] == {
        "required": False, "optional": True, "computed": False, "deprecated": False}


def test_a_deprecated_block_type_is_carried_through():
    schema = {"provider_schemas": {"p": {"resource_schemas": {"unifi_x": {"block": {
        "block_types": {"old": {"deprecated": True, "block": {}}}}}}}}}
    assert reduce_schema(schema)["unifi_x"]["old"]["deprecated"] is True


def test_every_detail_names_the_action_it_asks_for():
    # The detail is the whole product of the command: it has to say what
    # happened in words the operator can act on, not just carry a kind.
    def only(baseline, current):
        (f,) = diff_resources(baseline, current)
        return f

    assert only(_res(a={}), {}).detail.startswith("gone from the provider")
    assert only(_res(a={}, b={}), _res(a={})).detail.startswith("removed")
    new_attr = only(_res(a={}), _res(a={}, b={})).detail
    assert new_attr.startswith("new — plan against live")
    # Names the reason it cannot be more certain: the JSON has no defaults.
    assert "the schema JSON cannot show" in new_attr
    assert only(_res(a={}), _res(a={}, b={"required": True})).detail.startswith(
        "new and required")
    assert only(_res(a={}), _res(a={"required": True})).detail.startswith("now required")
    assert only(_res(a={}), _res(a={"deprecated": True})).detail.startswith("deprecated")


def test_every_finding_identifies_its_attribute():
    def only(baseline, current):
        (f,) = diff_resources(baseline, current)
        return f.identifier

    assert only(_res(a={}), _res(a={}, b={})) == "unifi_device.b"
    assert only(_res(a={}), _res(a={}, b={"required": True})) == "unifi_device.b"
    assert only(_res(a={}), _res(a={"required": True})) == "unifi_device.a"
    assert only(_res(a={}), _res(a={"deprecated": True})) == "unifi_device.a"


def test_a_new_resource_type_does_not_hide_later_findings():
    # The new-resource branch skips one type; it must not stop the walk. "a_"
    # sorts before "z_", so a break would swallow z_type's removal.
    baseline = {"z_type": {"gone": dict(_FLAGS)}}
    current = {"a_type": {"x": dict(_FLAGS)}, "z_type": {}}
    assert [f.identifier for f in diff_resources(baseline, current)] == ["z_type.gone"]


def test_a_removed_resource_type_does_not_hide_later_findings():
    # Same for the removed-resource branch: it skips the rest of THAT type's
    # attributes, not the rest of the provider.
    baseline = {"a_gone": {"x": dict(_FLAGS)}, "z_type": {"gone": dict(_FLAGS)}}
    current = {"z_type": {}}
    assert [f.identifier for f in diff_resources(baseline, current)] == [
        "a_gone", "z_type.gone"]


def test_an_irrelevant_finding_does_not_hide_a_relevant_one():
    # Both filter branches skip: an unused resource type, and a removed
    # attribute nothing sets. Neither may end the loop early.
    findings = diff_resources(
        {"a_unused": {"x": dict(_FLAGS)},
         "unifi_device": {"aaa_never_set": dict(_FLAGS),
                          "assisted_roaming_enabled": dict(_FLAGS)}},
        {"a_unused": {}, "unifi_device": {}},
    )
    kept = filter_to_config(findings, {"devices.tf": _DEVICES_TF})
    assert [f.identifier for f in kept] == ["unifi_device.assisted_roaming_enabled"]


def test_multiple_locations_are_listed_comma_separated():
    twice = _DEVICES_TF + _DEVICES_TF.replace('"sw"', '"sw2"')
    findings = diff_resources(_res(assisted_roaming_enabled={}), _res())
    (f,) = filter_to_config(findings, {"devices.tf": twice})
    assert f.detail.endswith("set in devices.tf:6, devices.tf:14")


# ---------------------------------------------------------------------------
# run_migrate: the IO shim. No controller is touched and nothing is written to
# the config — only the baseline file.
# ---------------------------------------------------------------------------

_LOCK_OLD = 'provider "registry.terraform.io/jamesbraid/unifi" {\n  version = "0.57.0"\n}\n'
_LOCK_NEW = 'provider "registry.terraform.io/jamesbraid/unifi" {\n  version = "0.101.1"\n}\n'


def _wire(monkeypatch, tmp_path, schema):
    """Point run_migrate at a canned provider schema in *tmp_path*."""
    import ubitofu.pipeline as pl

    class FakeRunner:
        def __init__(self, workdir):
            self.workdir = workdir

        def providers_schema(self):
            return schema

    monkeypatch.setattr(pl, "TofuRunner", lambda workdir: FakeRunner(workdir))
    return pl


def _cfg(tmp_path):
    from ubitofu.config import Config
    return Config("https://unifi.example", "default", "env", "UNIFI_API_KEY",
                  "ExampleVault", workdir=str(tmp_path))


def _run_migrate(monkeypatch, tmp_path, schema, **kw):
    import io
    pl = _wire(monkeypatch, tmp_path, schema)
    out = io.StringIO()
    rc = pl.run_migrate(_cfg(tmp_path), out, **kw)
    return rc, out.getvalue()


_OLD_SCHEMA = _schema({
    "name": {"type": "string", "required": True},
    "radio_table": {"optional": True, "nested_type": {
        "nesting_mode": "list",
        "attributes": {"assisted_roaming_enabled": {"type": "bool", "optional": True}},
    }},
})
_NEW_SCHEMA = _schema({"name": {"type": "string", "required": True}})


def test_first_run_records_a_baseline_and_exits_zero(monkeypatch, tmp_path):
    from ubitofu.pipeline import BASELINE_PATH
    (tmp_path / ".terraform.lock.hcl").write_text(_LOCK_OLD)
    rc, report = _run_migrate(monkeypatch, tmp_path, _OLD_SCHEMA)
    assert rc == 0
    assert "no baseline yet" in report
    assert (tmp_path / BASELINE_PATH).exists()


def test_write_baseline_refreshes_an_existing_one(monkeypatch, tmp_path):
    import json

    from ubitofu.pipeline import BASELINE_PATH
    (tmp_path / ".terraform.lock.hcl").write_text(_LOCK_OLD)
    _run_migrate(monkeypatch, tmp_path, _OLD_SCHEMA)
    (tmp_path / ".terraform.lock.hcl").write_text(_LOCK_NEW)
    rc, report = _run_migrate(monkeypatch, tmp_path, _NEW_SCHEMA, write_baseline=True)
    assert rc == 0
    assert "refreshed" in report
    saved = json.loads((tmp_path / BASELINE_PATH).read_text())
    assert saved["providers"] == {"registry.terraform.io/jamesbraid/unifi": "0.101.1"}
    assert "radio_table" not in saved["resources"]["unifi_device"]


def test_unchanged_provider_reports_clean_and_exits_zero(monkeypatch, tmp_path):
    (tmp_path / ".terraform.lock.hcl").write_text(_LOCK_OLD)
    (tmp_path / "devices.tf").write_text(_DEVICES_TF)
    _run_migrate(monkeypatch, tmp_path, _OLD_SCHEMA)
    rc, report = _run_migrate(monkeypatch, tmp_path, _OLD_SCHEMA)
    assert rc == 0
    assert "no config-visible schema changes" in report


def test_removed_attr_the_config_sets_blocks_the_bump(monkeypatch, tmp_path):
    """The 0.101.0 case end to end: the attribute is gone, the config still
    sets it, and `tofu plan` would fail with "Unsupported argument" before
    reconcile ever saw a plan."""
    (tmp_path / ".terraform.lock.hcl").write_text(_LOCK_OLD)
    (tmp_path / "devices.tf").write_text(_DEVICES_TF)
    _run_migrate(monkeypatch, tmp_path, _OLD_SCHEMA)
    (tmp_path / ".terraform.lock.hcl").write_text(_LOCK_NEW)
    rc, report = _run_migrate(monkeypatch, tmp_path, _NEW_SCHEMA)
    assert rc == 11
    assert "0.57.0 -> 0.101.1" in report
    assert "Blocking" in report
    assert "radio_table.assisted_roaming_enabled" in report
    assert "devices.tf:6" in report


def test_scaffold_files_are_not_read_as_committed_config(monkeypatch, tmp_path):
    # unifi-variables.tf is ubitofu's own output; a match there must not make a
    # removed attribute look like operator config.
    (tmp_path / ".terraform.lock.hcl").write_text(_LOCK_OLD)
    (tmp_path / "unifi-variables.tf").write_text(_DEVICES_TF)
    _run_migrate(monkeypatch, tmp_path, _OLD_SCHEMA)
    rc, _ = _run_migrate(monkeypatch, tmp_path, _NEW_SCHEMA)
    assert rc == 0


def test_missing_lock_file_is_not_an_error(monkeypatch, tmp_path):
    rc, report = _run_migrate(monkeypatch, tmp_path, _OLD_SCHEMA)
    assert rc == 0
    assert "no baseline yet" in report
