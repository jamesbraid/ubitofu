# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 James Braid
from pathlib import Path

import pytest

from ubitofu.hcl_surgeon import (
    _match_brace,
    _skip_block_comment,
    _skip_line_comment,
    _skip_string,
    _top_level_assignments,
    find_resource_block_span,
    update_scalar,
)

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


# ---------------------------------------------------------------------------
# Scanner primitives: exercised directly with real strings (no mocking). These
# pin the byte-level contracts that the higher-level round-trips rely on and
# that mutation testing probes at the boundary.
# ---------------------------------------------------------------------------

def test_skip_string_unterminated_stops_at_eof():
    # An unterminated string must return len(text) (loop bound is `i < n`),
    # never index past the end.
    assert _skip_string('"abc', 0) == 4


def test_skip_string_honours_escaped_quote():
    # `\"` is an escaped quote, not the terminator: the 6-char literal closes
    # only at its final quote.
    assert _skip_string('"a\\"b"', 0) == 6


def test_skip_string_returns_index_just_past_close():
    assert _skip_string('"a"', 0) == 3


def test_skip_line_comment_to_eof_returns_len():
    # No newline -> the comment runs to end of text.
    assert _skip_line_comment("# no newline", 0) == len("# no newline")


def test_skip_line_comment_returns_char_after_newline():
    assert _skip_line_comment("ab\ncd", 0) == 3


def test_match_brace_unterminated_returns_none():
    # No closing brace -> None, and the loop bound (`i < n`) must not index
    # past the end.
    assert _match_brace("{", 0) is None


def test_find_span_steps_over_hash_comment_with_brace():
    # A `#` line comment containing a `}` must not be miscounted as the block
    # close.
    text = 'resource "unifi_x" "a" {\n  # note with } brace\n  vlan = 1\n}\n'
    start, end = find_resource_block_span(text, "unifi_x", "a")
    assert "vlan = 1" in text[start:end]


def test_find_span_steps_over_slash_comment_with_brace():
    text = 'resource "unifi_x" "a" {\n  // note with } brace\n  vlan = 1\n}\n'
    start, end = find_resource_block_span(text, "unifi_x", "a")
    assert "vlan = 1" in text[start:end]


def test_find_span_steps_over_block_comment_with_brace():
    text = 'resource "unifi_x" "a" {\n  /* note } brace */\n  vlan = 1\n}\n'
    start, end = find_resource_block_span(text, "unifi_x", "a")
    assert "vlan = 1" in text[start:end]


def test_find_span_nested_block_depth_counts_up():
    # A nested `{ }` must raise depth to 2 (`depth += 1`), so the FIRST inner
    # close does not terminate the outer block; content after it survives.
    text = (
        'resource "unifi_x" "a" {\n'
        "  sub = {\n"
        "    x = 1\n"
        "  }\n"
        "  after = 2\n"
        "}\n"
    )
    start, end = find_resource_block_span(text, "unifi_x", "a")
    assert "after = 2" in text[start:end]


# ---------------------------------------------------------------------------
# _top_level_assignments: string/comment awareness and exact scanning.
# ---------------------------------------------------------------------------

def test_top_level_skips_bare_string_contents():
    # Text inside a string literal must not be scanned for assignments.
    d = _top_level_assignments('\n  "foo = bar"\n  vlan = 10\n')
    assert d.keys() == {"vlan"}


def test_top_level_skips_hash_comment_contents():
    d = _top_level_assignments('\n  # foo = bar\n  vlan = 10\n')
    assert d.keys() == {"vlan"}


def test_top_level_skips_slash_comment_contents():
    d = _top_level_assignments('\n  // foo = bar\n  vlan = 10\n')
    assert d.keys() == {"vlan"}


def test_top_level_skips_block_comment_contents():
    d = _top_level_assignments('\n  /* foo = bar */\n  vlan = 10\n')
    assert d.keys() == {"vlan"}


def test_top_level_open_bracket_set_is_exact():
    # Only real openers `{[(` raise depth; a stray letter (e.g. 'X') must not.
    d = _top_level_assignments('\n  Xray = 1\n  vlan = 10\n')
    assert d.keys() == {"Xray", "vlan"}


def test_top_level_close_returns_depth_to_zero_and_advances_one():
    # `( )` returns depth to exactly 0 (decrement by 1) advancing exactly one
    # char, so the following attr is captured intact.
    d = _top_level_assignments('\n  ( )vlan = 10\n')
    assert d.keys() == {"vlan"}


def test_top_level_ws_between_name_and_eq_is_only_space_tab():
    # A stray 'X' between a name and '=' is not whitespace: it breaks the
    # `vlan = ...` recognition, so `vlan` is NOT captured.
    d = _top_level_assignments('\n  vlan X = 10\n')
    assert "vlan" not in d


def test_top_level_bare_identifier_is_not_an_assignment():
    # A name with no `=` must not be recorded as an assignment.
    d = _top_level_assignments('\n  bare\n  vlan = 10\n')
    assert d.keys() == {"vlan"}


def test_top_level_double_equals_is_not_an_assignment():
    # `==` is guarded against: `vlan == 10` is not a scalar assignment.
    d = _top_level_assignments('\n  vlan == 10\n')
    assert "vlan" not in d


def test_top_level_value_leading_ws_is_only_space_tab():
    # A value starting with 'X' keeps the 'X' (it is not skippable whitespace).
    d = _top_level_assignments('\n  mode = Xauto\n')
    s, e = d["mode"]
    assert '\n  mode = Xauto\n'[s:e] == "Xauto"


# ---------------------------------------------------------------------------
# update_scalar round-trips pinning the finer scanning offsets.
# ---------------------------------------------------------------------------

def test_update_strips_slash_inline_comment_keeps_it():
    # A `//` trailing comment is excluded from the value span yet preserved.
    text = 'resource "unifi_x" "a" {\n  vlan = 10 // trailing note\n}\n'
    out = update_scalar(text, "unifi_x", "a", "vlan", 10, 11)
    assert out == 'resource "unifi_x" "a" {\n  vlan = 11 // trailing note\n}\n'


def test_update_string_value_escaped_quote_then_hash():
    # An escaped quote inside the value must be skipped so the following `#`
    # (still inside the string) is not treated as an inline comment.
    text = 'resource "unifi_x" "a" {\n  label = "a\\"# b"\n}\n'
    out = update_scalar(text, "unifi_x", "a", "label", 'a"# b', "new")
    assert 'label = "new"' in out


def test_update_string_value_close_quote_then_comment_no_space():
    # `"x"#c` — the value is exactly `"x"`; the `#c` is an inline comment.
    text = 'resource "unifi_x" "a" {\n  name = "x"#c\n}\n'
    out = update_scalar(text, "unifi_x", "a", "name", "x", "y")
    assert out == 'resource "unifi_x" "a" {\n  name = "y"#c\n}\n'


def test_update_attr_at_start_of_block_body():
    # No newline after `{`: the first attr begins at inner offset 0 (scan
    # starts at 0, not 1).
    text = 'resource "unifi_x" "a" {name = "v"\n}\n'
    out = update_scalar(text, "unifi_x", "a", "name", "v", "w")
    assert 'name = "w"' in out


def test_update_underscore_prefixed_attr():
    # `_` is a valid name-start character.
    text = 'resource "unifi_x" "a" {\n  _priv = 1\n}\n'
    out = update_scalar(text, "unifi_x", "a", "_priv", 1, 2)
    assert "_priv = 2" in out


def test_update_with_trailing_bare_identifier():
    # A bare identifier at the very end of the block body (name reaches the
    # body end) must not index past the end during the name scan.
    text = 'resource "unifi_x" "a" {\n  vlan = 10\n  end}\n'
    out = update_scalar(text, "unifi_x", "a", "vlan", 10, 11)
    assert "vlan = 11" in out


def test_update_with_trailing_identifier_and_spaces():
    # Trailing spaces after a bare identifier at body end must not index past
    # the end during the whitespace scan or the `=` probe.
    text = 'resource "unifi_x" "a" {\n  vlan = 10\n  end  }\n'
    out = update_scalar(text, "unifi_x", "a", "vlan", 10, 11)
    assert "vlan = 11" in out


def test_update_no_space_around_equals():
    # `vlan=10` — value begins exactly one char past `=`.
    text = 'resource "unifi_x" "a" {\n  vlan=10\n}\n'
    out = update_scalar(text, "unifi_x", "a", "vlan", 10, 11)
    assert "vlan=11" in out


def test_update_empty_value_to_body_end_raises_value_error():
    # `vlan =  ` (spaces to body end) yields an empty committed literal; the
    # anchor comparison fails with ValueError (not an IndexError from scanning
    # past the end).
    text = 'resource "unifi_x" "a" {\n  vlan =  }\n'
    with pytest.raises(ValueError):
        update_scalar(text, "unifi_x", "a", "vlan", 10, 11)


def test_update_value_at_body_end_no_newline():
    # `vlan = 10}` — no newline after the value; the whole `10` is the value.
    text = 'resource "unifi_x" "a" {\n  vlan = 10}\n'
    out = update_scalar(text, "unifi_x", "a", "vlan", 10, 11)
    assert "vlan = 11}" in out


def test_update_missing_resource_message():
    with pytest.raises(LookupError, match="not found"):
        update_scalar(_TWO, "unifi_network", "ghost", "vlan", 1, 2)


def test_update_missing_attr_message():
    with pytest.raises(LookupError, match="no top-level scalar attr"):
        update_scalar(_TWO, "unifi_network", "examplenet", "nonexistent", 1, 2)


def test_update_anchor_mismatch_message():
    with pytest.raises(ValueError, match="anchor mismatch"):
        update_scalar(_TWO, "unifi_network", "examplenet",
                      "name", "wrong-old", "renamed")


# ---------------------------------------------------------------------------
# _skip_block_comment: `/* … */` closing, exercised directly.
# ---------------------------------------------------------------------------

def test_skip_block_comment_search_starts_past_opener():
    # The `*/` search begins two chars in, so `/*/` is not self-closing and an
    # earlier stray `*/` is ignored.
    assert _skip_block_comment("/*/xx*/yy", 0) == 7


def test_skip_block_comment_empty_comment():
    # `/**/` closes immediately after the opener.
    assert _skip_block_comment("/**/xx", 0) == 4


def test_skip_block_comment_closes_at_first_not_last():
    # Two `*/` present: the comment closes at the FIRST (find, not rfind).
    assert _skip_block_comment("/* a */ b */ c", 0) == 7


def test_skip_block_comment_unterminated_returns_len():
    # No closing `*/` -> run to end of text.
    assert _skip_block_comment("/* unterminated", 0) == 15


def test_skip_block_comment_returns_two_past_close():
    # Closing offset is exactly `*/` end + 2.
    assert _skip_block_comment("/* x */yy", 0) == 7


def test_top_level_nested_open_increments_depth():
    # `(())` must raise depth to 2 (`depth += 1`), so both closers are needed to
    # return to 0 before the following attr is captured.
    d = _top_level_assignments('\n  (())after = 1\n')
    assert d.keys() == {"after"}


def test_top_level_open_advances_one_char():
    # After an opener the scanner advances exactly one char, so the immediately
    # following closer is still seen (depth returns to 0).
    d = _top_level_assignments('\n  ()after = 1\n')
    assert d.keys() == {"after"}
