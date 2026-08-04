# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 James Braid
"""Tests for transactional byte patches over indexed HCL."""

from __future__ import annotations

import pytest

from ubitofu.hcl_index import ByteSpan
from ubitofu.hcl_patches import BytePatch, apply_patches


def _patch(
    start: int,
    end: int,
    expected: bytes,
    replacement: bytes,
    reason: str = "test",
) -> BytePatch:
    return BytePatch(ByteSpan(start, end), expected, replacement, reason)


def test_noop_patch_is_byte_identical() -> None:
    """Catches rewriting or normalizing input for a patch that changes nothing."""
    source = b"before\r\nvalue\r\nafter\r\n"
    assert apply_patches(source, (_patch(8, 13, b"value", b"value"),)) == source


def test_scalar_replacement_changes_only_the_selected_bytes() -> None:
    """Catches replacement that consumes comment or whitespace beside an expression."""
    source = b'name = "old"  # retain\n'
    assert apply_patches(source, (_patch(7, 12, b'"old"', b'"new"'),)) == (
        b'name = "new"  # retain\n'
    )


def test_insertion_and_deletion_keep_the_unselected_bytes() -> None:
    """Catches incorrectly requiring nonempty spans or retaining deleted anchors."""
    source = b"one\ntwo\nthree\n"
    patches = (
        _patch(4, 4, b"", b"new\n"),
        _patch(8, 13, b"three", b""),
    )
    assert apply_patches(source, patches) == b"one\nnew\ntwo\n\n"


def test_adjacent_patches_apply_without_changing_their_boundaries() -> None:
    """Catches treating touching non-overlapping spans as a conflict."""
    source = b"abcdef"
    patches = (_patch(1, 3, b"bc", b"X"), _patch(3, 5, b"de", b"Y"))
    assert apply_patches(source, patches) == b"aXYf"


@pytest.mark.parametrize(
    "patches",
    [
        (_patch(1, 4, b"bcd", b"x"), _patch(3, 5, b"de", b"y")),
        (_patch(1, 3, b"bc", b"x"), _patch(1, 3, b"bc", b"y")),
        (_patch(1, 3, b"bc", b"x"), _patch(2, 2, b"", b"y")),
    ],
)
def test_overlap_or_duplicate_span_fails_before_producing_output(
    patches: tuple[BytePatch, ...],
) -> None:
    """Catches order-dependent edits whose patch anchors overlap."""
    with pytest.raises(ValueError, match="overlap|duplicate"):
        apply_patches(b"abcdef", patches)


@pytest.mark.parametrize(
    "patch",
    [
        _patch(-1, 0, b"", b""),
        _patch(4, 3, b"", b""),
        _patch(0, 7, b"abcdefg", b""),
        _patch(1, 3, b"WRONG", b"x"),
    ],
)
def test_invalid_bounds_or_expected_bytes_fail_before_any_patch_is_applied(
    patch: BytePatch,
) -> None:
    """Catches a partially-written candidate after an invalid later patch."""
    with pytest.raises(ValueError, match="bounds|expected"):
        apply_patches(b"abcdef", (_patch(0, 1, b"a", b"z"), patch))


def test_patches_apply_in_descending_offsets_independently_of_input_order() -> None:
    """Catches offset drift caused by applying a lower span before a higher span."""
    source = b"zero one two"
    first = _patch(5, 8, b"one", b"ONE!")
    second = _patch(9, 12, b"two", b"TWO")
    assert apply_patches(source, (first, second)) == b"zero ONE! TWO"
    assert apply_patches(source, (second, first)) == b"zero ONE! TWO"
