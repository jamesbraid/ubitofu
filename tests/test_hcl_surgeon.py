# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 James Braid
from pathlib import Path

import pytest

from ubitofu.hcl_surgeon import find_resource_block_span, update_scalar

_FIXTURES = Path(__file__).parent / "fixtures" / "reconcile"

# ---------------------------------------------------------------------------
# find_resource_block_span: brace-matched, comment/string aware.
# ---------------------------------------------------------------------------

_TWO = '''resource "unifi_network" "examplenet" {
  name    = "examplenet"  # inline comment stays
  enabled = true
  vlan    = 10
  dhcp_server = {
    enabled = true
    start   = "10.0.0.10"
  }
}

resource "unifi_network" "other" {
  name = "other"
  vlan = 20
}
'''


def test_find_span_locates_correct_block():
    start, end = find_resource_block_span(_TWO, "unifi_network", "examplenet")
    block = _TWO[start:end]
    assert block.startswith('resource "unifi_network" "examplenet"')
    assert block.endswith("}")
    # must not bleed into the second resource
    assert "other" not in block
    # brace matching stepped over the nested dhcp_server { } block
    assert "dhcp_server" in block


def test_find_span_second_block():
    start, end = find_resource_block_span(_TWO, "unifi_network", "other")
    block = _TWO[start:end]
    assert 'resource "unifi_network" "other"' in block
    assert "examplenet" not in block


def test_find_span_missing_returns_none():
    assert find_resource_block_span(_TWO, "unifi_network", "nope") is None
    assert find_resource_block_span(_TWO, "unifi_wlan", "examplenet") is None


def test_find_span_ignores_braces_inside_strings():
    text = 'resource "unifi_x" "a" {\n  name = "a{b}c"\n  v = 1\n}\n'
    start, end = find_resource_block_span(text, "unifi_x", "a")
    assert text[start:end].endswith("v = 1\n}")


# ---------------------------------------------------------------------------
# update_scalar: in-place value replace, everything else byte-identical.
# ---------------------------------------------------------------------------

def test_update_string_preserves_inline_comment():
    out = update_scalar(_TWO, "unifi_network", "examplenet",
                        "name", "examplenet", "renamed")
    assert 'name    = "renamed"  # inline comment stays' in out
    # only that one line changed; every other line identical
    assert out.replace('"renamed"', '"examplenet"') == _TWO


def test_update_number():
    out = update_scalar(_TWO, "unifi_network", "examplenet", "vlan", 10, 42)
    assert "vlan    = 42" in out
    assert "vlan    = 10" not in out
    # the OTHER resource's vlan = 20 must be untouched
    assert "vlan = 20" in out


def test_update_bool():
    out = update_scalar(_TWO, "unifi_network", "examplenet", "enabled", True, False)
    assert "enabled = false" in out
    assert "enabled = true\n  vlan" not in out


def test_update_only_touches_top_level_not_nested():
    # top-level `enabled = true` and nested dhcp_server.enabled = true both exist.
    out = update_scalar(_TWO, "unifi_network", "examplenet", "enabled", True, False)
    # nested block's enabled must remain true
    assert "    enabled = true\n    start" in out


def test_update_targets_correct_resource_of_same_type():
    out = update_scalar(_TWO, "unifi_network", "other", "vlan", 20, 99)
    assert "vlan = 99" in out
    assert "vlan    = 10" in out       # examplenet untouched


def test_update_string_with_quotes_and_escapes():
    text = 'resource "unifi_x" "a" {\n  label = "he said \\"hi\\""\n}\n'
    out = update_scalar(text, "unifi_x", "a", "label", 'he said "hi"', "bye")
    assert 'label = "bye"' in out


def test_update_anchor_mismatch_raises():
    # committed value is "examplenet"; claim old was "wrong" -> refuse to edit.
    with pytest.raises(ValueError):
        update_scalar(_TWO, "unifi_network", "examplenet",
                      "name", "wrong-old", "renamed")


def test_update_missing_resource_raises():
    with pytest.raises(LookupError):
        update_scalar(_TWO, "unifi_network", "ghost", "vlan", 1, 2)


def test_update_missing_attr_raises():
    with pytest.raises(LookupError):
        update_scalar(_TWO, "unifi_network", "examplenet", "nonexistent", 1, 2)


def test_update_idempotent_when_value_unchanged():
    # old == new -> text comes back byte-identical (no-op edit).
    out = update_scalar(_TWO, "unifi_network", "examplenet",
                        "vlan", 10, 10)
    assert out == _TWO


def test_update_preserves_leading_comment_lines():
    text = (
        'resource "unifi_network" "a" {\n'
        "  # operator note: keep this VLAN pinned\n"
        "  vlan = 10\n"
        "}\n"
    )
    out = update_scalar(text, "unifi_network", "a", "vlan", 10, 11)
    assert "# operator note: keep this VLAN pinned" in out
    assert "vlan = 11" in out


def test_update_float_scalar():
    text = 'resource "unifi_x" "a" {\n  ratio = 1.5\n}\n'
    out = update_scalar(text, "unifi_x", "a", "ratio", 1.5, 2.0)
    assert "ratio = 2" in out          # integral float renders without .0


def test_golden_before_to_after_byte_identical():
    """Applying the three scalar drifts to before.tf yields after.tf exactly —
    comments, the untouched nested dhcp_server block, and the second resource
    all survive byte-for-byte."""
    before = (_FIXTURES / "before.tf").read_text()
    after = (_FIXTURES / "after.tf").read_text()
    text = before
    text = update_scalar(text, "unifi_network", "core_lan", "name",
                         "Core LAN", "Core LAN renamed")
    text = update_scalar(text, "unifi_network", "core_lan", "vlan", 10, 11)
    text = update_scalar(text, "unifi_network", "core_lan", "enabled", True, False)
    assert text == after


def test_golden_idempotent_second_pass():
    """Re-applying with no drift (old == new) leaves after.tf byte-identical."""
    after = (_FIXTURES / "after.tf").read_text()
    text = after
    text = update_scalar(text, "unifi_network", "core_lan", "vlan", 11, 11)
    text = update_scalar(text, "unifi_network", "guest", "vlan", 20, 20)
    assert text == after
