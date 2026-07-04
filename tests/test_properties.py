# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 James Braid
"""Property-based tests for the HCL surgeon and slug-assignment invariants.

These encode the safety contract of hcl_surgeon.py and assign_slugs(), and
are deliberately written against the public interface so they validate any
future implementation (e.g. a tree-sitter backend).
"""

import pytest
from hypothesis import assume, given, settings
from hypothesis import strategies as st

from ubitofu.enumerator import ImportTarget
from ubitofu.hcl_surgeon import _serialize, find_resource_block_span, update_scalar  # noqa: F401
from ubitofu.import_emitter import assign_slugs

# _lit: the canonical scalar-to-HCL-literal helper.  Re-uses the surgeon's own
# _serialize so the property tests speak the surgeon's format, not a hand-rolled
# divergent one.
_lit = _serialize

# Strings in the surgeon's documented domain: no unescaped quotes, no
# backslashes, no newlines, no bare $ (heredocs / ${…} interpolations are
# explicitly out of scope for UniFi scalar values per the module docstring).
_safe_strings = st.text(
    alphabet=st.characters(blacklist_characters='"\\\n$'),
    min_size=0,
    max_size=30,
)

scalar_vals = st.one_of(
    _safe_strings,
    st.integers(min_value=-1000, max_value=100000),
    st.booleans(),
)


# ---------------------------------------------------------------------------
# (a) Idempotence: update_scalar with old == new returns byte-identical text.
# ---------------------------------------------------------------------------

@given(v=scalar_vals)
@settings(max_examples=200)
def test_update_scalar_idempotent_when_old_equals_new(v):
    text = f'resource "unifi_device" "x" {{\n  # keep me\n  name = {_lit(v)}\n}}\n'
    assert update_scalar(text, "unifi_device", "x", "name", v, v) == text


# ---------------------------------------------------------------------------
# (b) Byte-preservation: comments, other attrs, blank lines survive an edit.
# ---------------------------------------------------------------------------

@given(old=scalar_vals, new=scalar_vals)
@settings(max_examples=200)
def test_update_scalar_preserves_comments_and_other_lines(old, new):
    assume(old != new)
    text = (
        'resource "unifi_device" "x" {\n'
        '  # a comment above\n'
        f'  name = {_lit(old)}  # inline\n'
        '  mac  = "aa:bb"\n'
        '\n'
        '  # trailing comment\n'
        '}\n'
    )
    out = update_scalar(text, "unifi_device", "x", "name", old, new)
    assert "# a comment above" in out
    assert "# inline" in out
    assert 'mac  = "aa:bb"' in out
    assert "# trailing comment" in out
    assert _lit(new) in out


# ---------------------------------------------------------------------------
# (c) Anchor-safety: raises ValueError when committed literal != claimed old.
# ---------------------------------------------------------------------------

@given(new=scalar_vals)
def test_update_scalar_anchor_mismatch_raises(new):
    text = 'resource "unifi_device" "x" {\n  name = "committed"\n}\n'
    with pytest.raises(ValueError):
        update_scalar(text, "unifi_device", "x", "name", "WRONG_OLD", new)


# ---------------------------------------------------------------------------
# (d) Slug uniqueness: assign_slugs never collides intra-batch nor with reserved.
# ---------------------------------------------------------------------------

@given(
    names=st.lists(
        st.text(
            alphabet=st.characters(
                categories=(),
                include_characters="abcdefghijklmnopqrstuvwxyz0123456789_ ",
            ),
            min_size=1,
            max_size=20,
        ),
        min_size=1,
        max_size=8,
    ),
    reserved=st.sets(st.text(min_size=1, max_size=25), max_size=5),
)
def test_assign_slugs_never_collides_with_reserved_or_itself(names, reserved):
    targets = [ImportTarget("unifi_device", n, f"mac{i}") for i, n in enumerate(names)]
    res = {f"unifi_device.{r}" for r in reserved}
    out = assign_slugs(targets, reserved=res)
    slugs = [s for _, s in out]
    assert len(set(slugs)) == len(slugs)                        # intra-batch unique
    assert all(f"unifi_device.{s}" not in res for s in slugs)  # never reuse reserved
