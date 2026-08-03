# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 James Braid
# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "lark==1.3.1",
#   "python-hcl2==8.1.2",
#   "regex==2026.7.19",
#   "tree-sitter==0.26.0",
#   "tree-sitter-hcl==1.2.0",
# ]
# ///
"""Bounded original-byte span adapters for the HCL parser decision.

This is evidence code, not the production HCL index. It deliberately keeps
raw bytes as the source of truth and emits only generic structural spans.
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal


class ParserProofError(ValueError):
    """The candidate could not parse the input without recovery or ambiguity."""


@dataclass(frozen=True)
class Span:
    kind: Literal["block", "attribute", "reference"]
    address: str
    start_byte: int
    end_byte: int


@dataclass(frozen=True)
class CorpusResult:
    candidate: str
    passed: bool
    valid_cases: int
    rejected_cases: int
    expected_spans: int
    failures: tuple[str, ...]


def _corpus_source(case: dict[str, object], fixture_dir: Path) -> bytes:
    if "source_hex" in case:
        return bytes.fromhex(str(case["source_hex"]))
    return (fixture_dir / str(case["path"])).read_bytes()


def evaluate_corpus(
    candidate: str,
    adapter: Callable[[bytes], list[Span]],
    corpus_path: Path,
) -> CorpusResult:
    """Run one adapter against every literal expectation in the raw corpus."""
    cases = json.loads(corpus_path.read_text(encoding="utf-8"))["cases"]
    valid_cases = 0
    rejected_cases = 0
    expected_spans = 0
    failures: list[str] = []

    for case in cases:
        name = str(case["name"])
        source = _corpus_source(case, corpus_path.parent)
        if bool(case["accept"]):
            valid_cases += 1
            expected = [
                Span(
                    str(item["kind"]),
                    str(item["address"]),
                    int(item["start"]),
                    int(item["end"]),
                )
                for item in case["expected_spans"]
            ]
            expected_spans += len(expected)
            try:
                actual = adapter(source)
            except ParserProofError as exc:
                failures.append(f"{name}: unexpectedly rejected: {exc}")
                continue
            if actual != expected:
                failures.append(
                    f"{name}: ordered span mismatch: expected {expected!r}, got {actual!r}"
                )
        else:
            rejected_cases += 1
            try:
                adapter(source)
            except ParserProofError as exc:
                if str(case["error"]) not in str(exc):
                    failures.append(f"{name}: wrong rejection: {exc}")
            else:
                failures.append(f"{name}: accepted invalid input")

    return CorpusResult(
        candidate=candidate,
        passed=not failures,
        valid_cases=valid_cases,
        rejected_cases=rejected_cases,
        expected_spans=expected_spans,
        failures=tuple(failures),
    )


def _decode_source(raw: bytes) -> str:
    if raw.startswith(b"\xef\xbb\xbf"):
        raise ParserProofError("UTF-8 BOM is not accepted")
    try:
        return raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise ParserProofError(f"UTF-8 decode failed: {exc}") from None


def normalize_for_python_hcl2(raw: bytes) -> tuple[str, tuple[int, ...]]:
    """Return an LF parser view and every character boundary's raw byte offset."""
    decoded = _decode_source(raw)
    normalized: list[str] = []
    original_byte_boundaries = [0]
    byte_offset = 0
    char_offset = 0

    while char_offset < len(decoded):
        char = decoded[char_offset]
        if char == "\r":
            if char_offset + 1 >= len(decoded) or decoded[char_offset + 1] != "\n":
                raise ParserProofError("parse failed: bare carriage return")
            normalized.append("\n")
            byte_offset += 2
            char_offset += 2
        else:
            normalized.append(char)
            byte_offset += len(char.encode("utf-8"))
            char_offset += 1
        original_byte_boundaries.append(byte_offset)

    return "".join(normalized), tuple(original_byte_boundaries)


def _lark_identifier(node: Any) -> str:
    return str(node.children[0])


def _lark_spans(
    tree: Any,
    parser_text: str,
    byte_boundaries: tuple[int, ...],
) -> list[Span]:
    from lark import Tree

    spans: list[Span] = []

    def process_references(node: Tree, attribute_address: str) -> None:
        if node.data == "get_attr_expr_term":
            start_pos = node.meta.start_pos
            end_pos = node.meta.end_pos
            reference = parser_text[start_pos:end_pos]
            spans.append(
                Span(
                    "reference",
                    f"{attribute_address}::{reference}",
                    byte_boundaries[start_pos],
                    byte_boundaries[end_pos],
                )
            )
            return
        for child in node.children:
            if isinstance(child, Tree):
                process_references(child, attribute_address)

    def process_body(body: Tree, parent: tuple[str, ...]) -> None:
        for item in body.children:
            if not isinstance(item, Tree):
                continue
            if item.data == "attribute":
                name = _lark_identifier(item.children[0])
                address = ".".join((*parent, name))
                end_pos = item.meta.end_pos
                # python-hcl2's heredoc token owns the newline following the
                # closing marker; tree-sitter and the corpus define an
                # attribute as ending at the marker itself.
                if parser_text[item.meta.start_pos : end_pos].endswith("\n"):
                    end_pos -= 1
                spans.append(
                    Span(
                        "attribute",
                        address,
                        byte_boundaries[item.meta.start_pos],
                        byte_boundaries[end_pos],
                    )
                )
                for child in item.children[1:]:
                    if isinstance(child, Tree):
                        process_references(child, address)
            elif item.data == "block":
                identifier = _lark_identifier(item.children[0])
                labels: list[str] = []
                nested_body: Tree | None = None
                for child in item.children[1:]:
                    if not isinstance(child, Tree):
                        continue
                    if child.data in {"string", "string_lit"}:
                        literal = parser_text[child.meta.start_pos : child.meta.end_pos]
                        labels.append(str(json.loads(literal)))
                    elif child.data == "body":
                        nested_body = child
                block_path = (*parent, identifier, *labels)
                spans.append(
                    Span(
                        "block",
                        ".".join(block_path),
                        byte_boundaries[item.meta.start_pos],
                        byte_boundaries[item.meta.end_pos],
                    )
                )
                if nested_body is not None:
                    process_body(nested_body, block_path)

    if not isinstance(tree, Tree):
        raise ParserProofError("parse failed: python-hcl2 returned no tree")
    for child in tree.children:
        if isinstance(child, Tree) and child.data == "body":
            process_body(child, ())
    return spans


def parse_python_hcl2(raw: bytes) -> list[Span]:
    """Parse from raw bytes through python-hcl2's positioned Lark tree."""
    import hcl2

    parser_text, byte_boundaries = normalize_for_python_hcl2(raw)
    try:
        tree = hcl2.parses_to_tree(parser_text)
        return _lark_spans(tree, parser_text, byte_boundaries)
    except ParserProofError:
        raise
    except Exception as exc:
        raise ParserProofError(f"parse failed: python-hcl2: {exc}") from None


def _tree_sitter_spans(root: Any, raw: bytes) -> list[Span]:
    spans: list[Span] = []

    def fail_on_recovery(node: Any) -> None:
        node_type = str(node.type)
        if bool(node.is_error) or bool(node.is_missing) or node_type in {"ERROR", "MISSING"}:
            raise ParserProofError(
                f"parse failed: tree-sitter-hcl recovered {node_type} at "
                f"bytes {node.start_byte}:{node.end_byte}"
            )
        for child in node.children:
            fail_on_recovery(child)

    def node_text(node: Any) -> str:
        start = int(node.start_byte)
        end = int(node.end_byte)
        return raw[start:end].decode("utf-8")

    def process_body(body: Any, parent: tuple[str, ...]) -> None:
        def process_references(node: Any, attribute_address: str) -> None:
            named_children = list(node.named_children)
            index = 0
            while index < len(named_children):
                child = named_children[index]
                if child.type == "variable_expr":
                    end_index = index + 1
                    while (
                        end_index < len(named_children)
                        and named_children[end_index].type == "get_attr"
                    ):
                        end_index += 1
                    if end_index > index + 1:
                        end_node = named_children[end_index - 1]
                        start_byte = int(child.start_byte)
                        end_byte = int(end_node.end_byte)
                        reference = raw[start_byte:end_byte].decode("utf-8")
                        spans.append(
                            Span(
                                "reference",
                                f"{attribute_address}::{reference}",
                                start_byte,
                                end_byte,
                            )
                        )
                        index = end_index
                        continue
                process_references(child, attribute_address)
                index += 1

        for item in body.named_children:
            item_type = str(item.type)
            named_children = list(item.named_children)
            if item_type == "attribute":
                identifier = next(child for child in named_children if child.type == "identifier")
                address = ".".join((*parent, node_text(identifier)))
                spans.append(
                    Span(
                        "attribute",
                        address,
                        int(item.start_byte),
                        int(item.end_byte),
                    )
                )
                expression = next(
                    (child for child in named_children if child.type == "expression"), None
                )
                if expression is not None:
                    process_references(expression, address)
            elif item_type == "block":
                identifier = next(child for child in named_children if child.type == "identifier")
                labels = [
                    str(json.loads(node_text(child)))
                    for child in named_children
                    if child.type == "string_lit"
                ]
                block_path = (*parent, node_text(identifier), *labels)
                spans.append(
                    Span(
                        "block",
                        ".".join(block_path),
                        int(item.start_byte),
                        int(item.end_byte),
                    )
                )
                nested_body = next(
                    (child for child in named_children if child.type == "body"), None
                )
                if nested_body is not None:
                    process_body(nested_body, block_path)

    fail_on_recovery(root)
    for child in root.named_children:
        if child.type == "body":
            process_body(child, ())
    return spans


def parse_tree_sitter(raw: bytes) -> list[Span]:
    """Parse native bytes and reject every recovered ERROR or MISSING node."""
    _decode_source(raw)

    import tree_sitter_hcl
    from tree_sitter import Language, Parser

    parser = Parser(Language(tree_sitter_hcl.language()))
    tree = parser.parse(raw)
    try:
        return _tree_sitter_spans(tree.root_node, raw)
    except ParserProofError:
        raise
    except Exception as exc:
        raise ParserProofError(f"parse failed: tree-sitter-hcl: {exc}") from None


def main(argv: list[str] | None = None) -> int:
    """Evaluate the pinned candidates and print the recorded selection as JSON."""
    repository_root = Path(__file__).resolve().parents[1]
    default_corpus = repository_root / "tests" / "fixtures" / "hcl" / "corpus.json"
    argument_parser = argparse.ArgumentParser(description=__doc__)
    argument_parser.add_argument("--corpus", type=Path, default=default_corpus)
    arguments = argument_parser.parse_args(argv)

    results = (
        evaluate_corpus("python-hcl2", parse_python_hcl2, arguments.corpus),
        evaluate_corpus("tree-sitter-hcl", parse_tree_sitter, arguments.corpus),
    )
    selection_matches = not results[0].passed and results[1].passed
    report = {
        "selected": "tree-sitter-hcl" if selection_matches else None,
        "results": [asdict(result) for result in results],
    }
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if selection_matches else 1


if __name__ == "__main__":
    raise SystemExit(main())
