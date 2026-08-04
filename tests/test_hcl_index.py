# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 James Braid
"""Behavioral tests for the native HCL structural index."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path, PurePosixPath

import pytest

from ubitofu.hcl_index import BlockKey, ByteSpan, index_hcl

_FIXTURES = Path(__file__).parent / "fixtures" / "hcl"
_CASES = json.loads((_FIXTURES / "corpus.json").read_text(encoding="utf-8"))["cases"]


def _source(case: dict[str, object]) -> bytes:
    if "source_hex" in case:
        return bytes.fromhex(str(case["source_hex"]))
    return (_FIXTURES / str(case["path"])).read_bytes()


def _spans(source: bytes) -> list[tuple[str, str, int, int]]:
    index = index_hcl(path=PurePosixPath("fixture.tf"), source=source)
    result: list[tuple[str, str, int, int]] = []
    for block in index.blocks:
        result.append(("block", _block_address(block.key), block.whole.start, block.whole.end))
    for attribute in index.attributes:
        result.append(
            (
                "attribute",
                f"{_block_address(attribute.block)}.{attribute.name}",
                attribute.whole.start,
                attribute.whole.end,
            )
        )
    for reference in index.references:
        block, name = reference.attribute
        result.append(
            (
                "reference",
                f"{_block_address(block)}.{name}::{_traversal_address(reference.traversal)}",
                reference.expression.start,
                reference.expression.end,
            )
        )
    return sorted(result, key=lambda span: span[2])


def _block_address(key: BlockKey) -> str:
    parts: list[str] = []
    for kind, labels in key.parents:
        parts.extend((kind, *labels))
    parts.extend((key.kind, *key.labels))
    return ".".join(parts)


def _traversal_address(traversal: tuple[str | int, ...]) -> str:
    return ".".join(str(part) for part in traversal)


@pytest.mark.parametrize(
    "case",
    [case for case in _CASES if case["accept"]],
    ids=[str(case["name"]) for case in _CASES if case["accept"]],
)
def test_index_matches_the_complete_literal_original_byte_corpus(case: dict[str, object]) -> None:
    """Catches lost, reordered, duplicate, and normalized source coordinates."""
    source = _source(case)
    expected = [
        (str(item["kind"]), str(item["address"]), int(item["start"]), int(item["end"]))
        for item in case["expected_spans"]  # type: ignore[union-attr]
    ]

    assert _spans(source) == expected


@pytest.mark.parametrize(
    "case",
    [case for case in _CASES if not case["accept"]],
    ids=[str(case["name"]) for case in _CASES if not case["accept"]],
)
def test_index_rejects_every_invalid_corpus_case(case: dict[str, object]) -> None:
    """Catches accepting tree-sitter recovery nodes as editable syntax."""
    with pytest.raises(ValueError, match=str(case["error"])):
        index_hcl(path=PurePosixPath("invalid.tf"), source=_source(case))


def test_index_rejects_invalid_utf8_before_it_can_be_sliced() -> None:
    """Catches parser output whose byte coordinates cannot name UTF-8 tokens."""
    with pytest.raises(ValueError, match="UTF-8"):
        index_hcl(path=PurePosixPath("invalid.tf"), source=b'resource "x" "bad" { value = "\xff" }')


def test_index_keeps_nested_parent_keys_and_raw_body_boundaries() -> None:
    """Catches flattening nested blocks or including delimiters in editable bodies."""
    source = b'resource "widget" "edge" {\n  lifecycle {\n    enabled = true\n  }\n}\n'
    index = index_hcl(path=PurePosixPath("network.tf"), source=source)
    lifecycle = next(block for block in index.blocks if block.key.kind == "lifecycle")

    assert lifecycle.key == BlockKey(
        kind="lifecycle",
        labels=(),
        parents=(("resource", ("widget", "edge")),),
    )
    assert source[lifecycle.body.start : lifecycle.body.end] == b"enabled = true"


def test_index_records_source_hash_from_unmodified_bytes() -> None:
    """Catches hashing decoded or newline-normalized source instead of patch input."""
    source = b'resource "x" "unicode" {\r\n  value = "\xce\xbb"\r\n}\r\n'
    index = index_hcl(path=PurePosixPath("unicode.tf"), source=source)

    assert index.source_sha256 == hashlib.sha256(source).hexdigest()
    assert all(0 <= span.start <= span.end <= len(source) for span in _all_spans(index))


def _all_spans(index: object) -> tuple[ByteSpan, ...]:
    from ubitofu.hcl_index import HclIndex

    assert isinstance(index, HclIndex)
    return (
        tuple(span for block in index.blocks for span in (block.whole, block.body))
        + tuple(
            span
            for attribute in index.attributes
            for span in (attribute.whole, attribute.expression)
        )
        + tuple(reference.expression for reference in index.references)
    )
