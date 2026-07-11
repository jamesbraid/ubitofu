# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 James Braid
import json

import pytest

from ubitofu.coverage import (
    Finding,
    _norm,
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
