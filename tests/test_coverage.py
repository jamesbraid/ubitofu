# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 James Braid
import io
import json
import random

import httpx
import pytest
from hypothesis import given
from hypothesis import strategies as st

from ubitofu.controller import Controller
from ubitofu.coverage import (
    CoverageReport,
    Finding,
    _norm,
    audit,
    audit_endpoints,
    audit_guest_networks,
    audit_manifest_lag,
    audit_settings,
    render_coverage_md,
    schema_resource_types,
    setting_schema_sections,
    write_coverage_md,
)
from ubitofu.pipeline import _emit_coverage


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


# Endpoint audit tests


class FakeCoverageController(Controller):
    def __init__(self, populated=None, errors=None):
        self.site = "default"
        self._populated = populated or {}   # endpoint -> list of objects
        self._errors = errors or {}         # endpoint -> HTTP status code

    def collection(self, endpoint):
        if endpoint in self._errors:
            code = self._errors[endpoint]
            raise httpx.HTTPStatusError(
                f"HTTP {code}",
                request=httpx.Request("GET", "https://unifi.example/x"),
                response=httpx.Response(code, request=httpx.Request(
                    "GET", "https://unifi.example/x")),
            )
        return self._populated.get(endpoint, [])


def test_populated_unmapped_endpoint_is_a_gap():
    ctl = FakeCoverageController(populated={
        "v2/api/site/{site}/nat": [{"_id": "n1", "type": "MASQUERADE"}]})
    gaps, accepted = audit_endpoints(ctl)
    nat = [f for f in gaps if f.identifier == "v2/api/site/{site}/nat"]
    assert len(nat) == 1
    assert "1 object(s)" in nat[0].detail and "NAT rules" in nat[0].detail


def test_default_objects_are_accepted_not_gaps():
    ctl = FakeCoverageController(populated={
        "rest/wlangroup": [
            {"_id": "a", "name": "Default", "attr_no_delete": True},
            {"_id": "b", "name": "Off", "attr_hidden_id": "Off"},
        ]})
    gaps, accepted = audit_endpoints(ctl)
    assert not [f for f in gaps if f.identifier == "rest/wlangroup"]
    wg = [f for f in accepted if f.identifier == "rest/wlangroup"]
    assert len(wg) == 1 and "2 built-in default object(s)" in wg[0].detail


def test_missing_endpoint_is_accepted_with_status():
    ctl = FakeCoverageController(errors={"rest/hotspot2conf": 404})
    gaps, accepted = audit_endpoints(ctl)
    h2 = [f for f in accepted if f.identifier == "rest/hotspot2conf"]
    assert len(h2) == 1 and "HTTP 404" in h2[0].detail
    assert not [f for f in gaps if f.identifier == "rest/hotspot2conf"]


def test_empty_endpoint_reports_nothing():
    gaps, accepted = audit_endpoints(FakeCoverageController())
    assert not gaps and not accepted


def test_manifest_mapped_endpoints_are_never_probed():
    # If a PROBE endpoint gains a MANIFEST spec, the probe must skip it.
    from ubitofu.manifest import ResourceSpec
    spec = ResourceSpec("unifi_nat_rule", "v2/api/site/{site}/nat", "_id")
    ctl = FakeCoverageController(populated={
        "v2/api/site/{site}/nat": [{"_id": "n1"}]})
    gaps, _ = audit_endpoints(ctl, manifest=(spec,))
    assert not [f for f in gaps if f.identifier == "v2/api/site/{site}/nat"]


def test_manifest_lag_flags_unmapped_provider_resources(schema):
    # Fixture schema has unifi_ap_group; MANIFEST does not map it (yet).
    findings = audit_manifest_lag(schema)
    assert any(f.identifier == "unifi_ap_group" and f.kind == "resource"
               for f in findings)
    # unifi_network IS in MANIFEST — never flagged.
    assert not any(f.identifier == "unifi_network" for f in findings)


def test_guest_networks_reported_while_discriminator_excludes_them():
    ctl = FakeCoverageController(populated={
        "rest/networkconf": [
            {"_id": "g1", "name": "guest", "purpose": "guest"},
            {"_id": "c1", "name": "lan", "purpose": "corporate"},
        ]})
    findings = audit_guest_networks(ctl)
    assert len(findings) == 1
    assert "1 guest network(s)" in findings[0].detail


def test_no_guest_networks_no_finding():
    ctl = FakeCoverageController(populated={
        "rest/networkconf": [{"_id": "c1", "purpose": "corporate"}]})
    assert audit_guest_networks(ctl) == []


def test_audit_combines_all_checks(schema):
    ctl = FakeCoverageController(populated={
        "get/setting": [_settings_record("mdns", enabled_for="some"),
                        _settings_record("super_mgmt", enable_analytics=True)],
        "v2/api/site/{site}/nat": [{"_id": "n1"}],
        "rest/networkconf": [{"_id": "g1", "purpose": "guest"}],
    })
    report = audit(ctl, schema)
    assert isinstance(report, CoverageReport)
    kinds = {(f.kind, f.identifier) for f in report.gaps}
    assert ("section", "mdns") in kinds
    assert ("endpoint", "v2/api/site/{site}/nat") in kinds
    assert ("resource", "unifi_ap_group") in kinds
    assert ("object", "unifi_network") in kinds
    assert [f.identifier for f in report.accepted] == ["super_mgmt"]


# Coverage rendering tests


def _report():
    return CoverageReport(
        gaps=[
            Finding("section", "mdns", "live config (2 field(s)); "
                    "provider unifi_setting lacks it"),
            Finding("endpoint", "v2/api/site/{site}/nat",
                    "1 object(s) (NAT rules) with no provider resource"),
        ],
        accepted=[Finding("section", "super_mgmt",
                          "console-scope; a site-scoped unifi_setting "
                          "cannot model it")],
    )


def test_render_coverage_md_golden():
    md = render_coverage_md(_report())
    assert md.startswith("# Provider coverage\n")
    assert "## Gaps" in md and "## Accepted" in md
    assert "- section mdns:" in md
    assert "- endpoint v2/api/site/{site}/nat:" in md
    assert "- section super_mgmt:" in md
    assert "do not edit" in md.lower()


def test_render_coverage_md_is_byte_stable_under_input_order():
    r1, r2 = _report(), _report()
    random.Random(1).shuffle(r2.gaps)
    random.Random(2).shuffle(r2.accepted)
    assert render_coverage_md(r1) == render_coverage_md(r2)


def test_render_coverage_md_empty_report():
    md = render_coverage_md(CoverageReport())
    assert "None." in md  # both sections render explicitly, never omitted


def test_write_coverage_md(tmp_path):
    write_coverage_md(tmp_path, _report())
    text = (tmp_path / "COVERAGE.md").read_text()
    assert text == render_coverage_md(_report())


# _emit_coverage seam tests


def test_emit_coverage_writes_file_and_prints(schema, tmp_path):
    ctl = FakeCoverageController(populated={
        "get/setting": [_settings_record("mdns", enabled_for="some")]})
    buf = io.StringIO()
    _emit_coverage(ctl, schema, tmp_path, ["unifi_bgp skipped — not configured"],
                   buf)
    text = (tmp_path / "COVERAGE.md").read_text()
    assert "- section mdns:" in text
    printed = buf.getvalue()
    assert "unifi_bgp skipped" in printed  # enumeration gaps share the section
    assert "section mdns:" in printed


def test_emit_coverage_is_byte_stable_across_runs(schema, tmp_path):
    ctl = FakeCoverageController(populated={
        "get/setting": [_settings_record("mdns", enabled_for="some")]})
    _emit_coverage(ctl, schema, tmp_path, [], io.StringIO())
    first = (tmp_path / "COVERAGE.md").read_bytes()
    _emit_coverage(ctl, schema, tmp_path, [], io.StringIO())
    assert (tmp_path / "COVERAGE.md").read_bytes() == first
