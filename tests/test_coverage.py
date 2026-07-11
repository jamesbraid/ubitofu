# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 James Braid
import json

import pytest
from hypothesis import given
from hypothesis import strategies as st

from ubitofu.coverage import (
    Finding,
    _norm,
    audit_settings,
    schema_resource_types,
    setting_schema_sections,
)


@pytest.fixture
def schema(fixtures_dir):
    return json.loads(
        (fixtures_dir / "coverage" / "providers_schema.json").read_text())


def test_norm_matches_camelcase_to_snake():
    assert _norm("fingerprintingEnabled") == _norm("fingerprinting_enabled")


def test_setting_schema_sections_extracts_nested_fields(schema):
    sections = setting_schema_sections(schema)
    assert sections["mgmt"] == {_norm("led_enabled"), _norm("ssh_enabled")}
    assert _norm("fingerprintingEnabled") in sections["dpi"]


def test_setting_schema_sections_excludes_non_sections(schema):
    sections = setting_schema_sections(schema)
    for meta in ("site", "id", "timeouts"):
        assert meta not in sections


def test_setting_schema_sections_raises_without_unifi_setting():
    with pytest.raises(KeyError):
        setting_schema_sections({"provider_schemas": {}})


def test_schema_resource_types(schema):
    assert schema_resource_types(schema) == {
        "unifi_setting", "unifi_network", "unifi_ap_group"}


def test_finding_line_is_deterministic():
    f = Finding("section", "mdns", "live config; provider unifi_setting lacks it")
    assert f.line() == "section mdns: live config; provider unifi_setting lacks it"


def _sections(schema, fixtures_dir=None):
    return setting_schema_sections(schema)


def _settings_record(key, **body):
    return {"key": key, "_id": "x", "site_id": "s", **body}


def test_uncovered_section_with_config_is_a_gap(schema):
    gaps, accepted = audit_settings(
        [_settings_record("mdns", enabled_for="some")], _sections(schema))
    assert [f.identifier for f in gaps] == ["mdns"]
    assert gaps[0].kind == "section"
    assert not accepted


def test_covered_section_with_known_fields_is_clean(schema):
    gaps, accepted = audit_settings(
        [_settings_record("mgmt", led_enabled=True)], _sections(schema))
    assert not gaps and not accepted


def test_unknown_field_in_covered_section_is_a_field_gap(schema):
    gaps, _ = audit_settings(
        [_settings_record("mgmt", led_enabled=True, x_mgmt_key="REDACTED")],
        _sections(schema))
    assert [f.identifier for f in gaps] == ["mgmt.x_mgmt_key"]
    assert gaps[0].kind == "field"


def test_camelcase_live_field_matches_snake_schema(schema):
    gaps, _ = audit_settings(
        [_settings_record("dpi", fingerprintingEnabled=True)], _sections(schema))
    assert not gaps


def test_rsyslogd_alias_maps_to_syslog(schema):
    gaps, _ = audit_settings(
        [_settings_record("rsyslogd", enabled=True, ip="10.0.0.1")],
        _sections(schema))
    assert not gaps


def test_ips_suppression_maps_into_ips_with_prefix(schema):
    gaps, _ = audit_settings(
        [_settings_record("ips_suppression", whitelist=[], alerts=[])],
        _sections(schema))
    assert not gaps


def test_classified_section_is_accepted_with_reason(schema):
    gaps, accepted = audit_settings(
        [_settings_record("super_mgmt", autobackup_enabled=True)],
        _sections(schema))
    assert not gaps
    assert [f.identifier for f in accepted] == ["super_mgmt"]
    assert "console-scope" in accepted[0].detail


def test_empty_section_body_is_not_reported(schema):
    gaps, accepted = audit_settings(
        [_settings_record("super_cloudaccess")], _sections(schema))
    assert not gaps and not accepted


_key = st.text(alphabet="abcdefgh_", min_size=1, max_size=12)


@given(st.lists(
    st.tuples(_key, st.dictionaries(_key, st.booleans(), max_size=3)),
    max_size=8))
def test_every_live_section_lands_in_at_most_one_bucket(sections):
    live = [_settings_record(k, **body) for k, body in sections]
    schema_sections = {"mgmt": {_norm("led_enabled")}}
    gaps, accepted = audit_settings(live, schema_sections)
    gap_sections = {f.identifier for f in gaps if f.kind == "section"}
    accepted_sections = {f.identifier for f in accepted}
    assert not gap_sections & accepted_sections
    for k, body in sections:
        if not body:
            continue  # empty body: nothing to manage, legitimately absent
        in_schema = k in schema_sections  # covered -> field checks, no section line
        assert in_schema or (k in gap_sections) or (k in accepted_sections)
