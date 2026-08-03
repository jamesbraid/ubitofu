# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 James Braid
"""Disposable parser comparison against literal original-byte expectations."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import pytest
from tools.hcl_parser_proof import (
    ParserProofError,
    Span,
    evaluate_corpus,
    main,
    normalize_for_python_hcl2,
    parse_python_hcl2,
    parse_tree_sitter,
)

_FIXTURES = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "hcl"
_CORPUS = json.loads((_FIXTURES / "corpus.json").read_text(encoding="utf-8"))["cases"]
_ADAPTERS: tuple[tuple[str, Callable[[bytes], list[Span]]], ...] = (
    ("python-hcl2", parse_python_hcl2),
    ("tree-sitter-hcl", parse_tree_sitter),
)


def _source(case: dict[str, object]) -> bytes:
    if "source_hex" in case:
        return bytes.fromhex(str(case["source_hex"]))
    return (_FIXTURES / str(case["path"])).read_bytes()


@pytest.mark.parametrize(
    "case",
    [case for case in _CORPUS if case["accept"]],
    ids=[str(case["name"]) for case in _CORPUS if case["accept"]],
)
def test_valid_corpus_has_literal_original_byte_spans(
    case: dict[str, object],
) -> None:
    """Catches missing, extra, reordered, duplicated, or mispositioned spans."""
    actual = parse_tree_sitter(_source(case))
    expected = [
        Span(str(item["kind"]), str(item["address"]), int(item["start"]), int(item["end"]))
        for item in case["expected_spans"]  # type: ignore[union-attr]
    ]
    assert actual == expected


@pytest.mark.parametrize("adapter_name,adapter", _ADAPTERS)
@pytest.mark.parametrize(
    "case",
    [case for case in _CORPUS if not case["accept"]],
    ids=[str(case["name"]) for case in _CORPUS if not case["accept"]],
)
def test_invalid_corpus_fails_closed(
    adapter_name: str,
    adapter: Callable[[bytes], list[Span]],
    case: dict[str, object],
) -> None:
    """Catches recovered tree-sitter errors, Lark partial parses, and BOM drift."""
    del adapter_name
    with pytest.raises(ParserProofError, match=str(case["error"])):
        adapter(_source(case))


@pytest.mark.parametrize("_adapter_name,adapter", _ADAPTERS)
def test_invalid_utf8_fails_closed(
    _adapter_name: str, adapter: Callable[[bytes], list[Span]]
) -> None:
    with pytest.raises(ParserProofError, match="UTF-8"):
        adapter(b'resource "x" "bad" { value = "\xff" }')


def test_python_hcl2_normalization_has_total_original_byte_boundary_map() -> None:
    """Catches maps based on Unicode characters or normalized byte counts."""
    text, original_byte_boundaries = normalize_for_python_hcl2(b"A\r\n\xc3\xa9\n")
    assert text == "A\né\n"
    assert original_byte_boundaries == (0, 1, 3, 5, 6)
    assert len(original_byte_boundaries) == len(text) + 1


@pytest.mark.parametrize("_adapter_name,adapter", _ADAPTERS)
def test_heredoc_string_and_comment_decoys_are_not_blocks(
    _adapter_name: str, adapter: Callable[[bytes], list[Span]]
) -> None:
    source = (_FIXTURES / "structural_edge_cases.tf").read_bytes()
    addresses = {span.address for span in adapter(source)}
    assert "resource.unifi_network.decoy" not in addresses
    assert "resource.unifi_network.heredoc_decoy" not in addresses
    assert "resource.unifi_network.comment_decoy" not in addresses


def test_tree_sitter_candidate_matches_complete_corpus() -> None:
    result = evaluate_corpus("tree-sitter-hcl", parse_tree_sitter, _FIXTURES / "corpus.json")
    assert result.passed is True
    assert result.valid_cases == 8
    assert result.rejected_cases == 3
    assert result.expected_spans == 53
    assert result.failures == ()


def test_python_hcl2_candidate_reports_heredoc_reference_gap() -> None:
    result = evaluate_corpus("python-hcl2", parse_python_hcl2, _FIXTURES / "corpus.json")
    assert result.passed is False
    assert result.valid_cases == 8
    assert result.rejected_cases == 3
    assert result.expected_spans == 53
    assert len(result.failures) == 1
    assert result.failures[0].startswith("structural_lf: ordered span mismatch")


def test_evaluator_rejects_an_unexpected_duplicate_span() -> None:
    def duplicate_adapter(source: bytes) -> list[Span]:
        spans = parse_tree_sitter(source)
        return [*spans, spans[0]]

    result = evaluate_corpus("duplicate", duplicate_adapter, _FIXTURES / "corpus.json")
    assert result.passed is False
    assert len(result.failures) == 8


def test_command_reports_the_bounded_selection(capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = main(["--corpus", str(_FIXTURES / "corpus.json")])

    report = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert report["selected"] == "tree-sitter-hcl"
    assert [
        (result["candidate"], result["passed"], result["expected_spans"])
        for result in report["results"]
    ] == [
        ("python-hcl2", False, 53),
        ("tree-sitter-hcl", True, 53),
    ]
