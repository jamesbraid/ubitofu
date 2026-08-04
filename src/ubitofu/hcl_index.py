# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 James Braid
"""Raw-byte structural spans for valid native HCL source.

This module deliberately exposes positions only from tree-sitter's original
byte input.  It is an internal seam until the single public-path cutover.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any

import tree_sitter_hcl
from tree_sitter import Language, Parser


@dataclass(frozen=True, order=True)
class ByteSpan:
    """A half-open interval into an unmodified source byte string."""

    start: int
    end: int


@dataclass(frozen=True, order=True)
class BlockKey:
    """A block header together with its containing block headers."""

    kind: str
    labels: tuple[str, ...]
    parents: tuple[tuple[str, tuple[str, ...]], ...]


@dataclass(frozen=True)
class BlockSpan:
    """Original-byte extent and editable body of a native HCL block."""

    key: BlockKey
    whole: ByteSpan
    body: ByteSpan


@dataclass(frozen=True)
class AttributeSpan:
    """Original-byte extent and expression of a native HCL attribute."""

    block: BlockKey
    name: str
    whole: ByteSpan
    expression: ByteSpan


@dataclass(frozen=True)
class ReferenceSpan:
    """A qualified expression traversal found in an attribute expression."""

    attribute: tuple[BlockKey, str]
    traversal: tuple[str | int, ...]
    expression: ByteSpan


@dataclass(frozen=True)
class HclIndex:
    """Structural native-HCL facts extracted without changing source bytes."""

    source_sha256: str
    blocks: tuple[BlockSpan, ...]
    attributes: tuple[AttributeSpan, ...]
    references: tuple[ReferenceSpan, ...]


_INTEGER = re.compile(r"-?(?:0|[1-9][0-9]*)$")
_PARSER = Parser(Language(tree_sitter_hcl.language()))


def index_hcl(*, path: PurePosixPath, source: bytes) -> HclIndex:
    """Index valid UTF-8 native HCL with positions against *source* itself."""
    del path  # Retained in the interface so future diagnostics can name sources.
    _validate_utf8(source)
    root = _PARSER.parse(source).root_node
    _reject_recovery(root)

    blocks: list[BlockSpan] = []
    attributes: list[AttributeSpan] = []
    references: list[ReferenceSpan] = []
    for child in root.named_children:
        if child.type == "body":
            _index_body(
                child,
                source=source,
                parents=(),
                blocks=blocks,
                attributes=attributes,
                references=references,
            )
    return HclIndex(
        source_sha256=hashlib.sha256(source).hexdigest(),
        blocks=tuple(blocks),
        attributes=tuple(attributes),
        references=tuple(references),
    )


def _validate_utf8(source: bytes) -> None:
    if source.startswith(b"\xef\xbb\xbf"):
        raise ValueError("UTF-8 BOM is not accepted")
    try:
        source.decode("utf-8", errors="strict")
    except UnicodeDecodeError as error:
        raise ValueError("UTF-8 decode failed") from error


def _reject_recovery(node: Any) -> None:
    if node.is_error or node.is_missing or node.type in {"ERROR", "MISSING"}:
        raise ValueError(f"parse failed: recovered {node.type}")
    for child in node.children:
        _reject_recovery(child)


def _index_body(
    body: Any,
    *,
    source: bytes,
    parents: tuple[tuple[str, tuple[str, ...]], ...],
    blocks: list[BlockSpan],
    attributes: list[AttributeSpan],
    references: list[ReferenceSpan],
) -> None:
    for item in body.named_children:
        if item.type == "block":
            _index_block(
                item,
                source=source,
                parents=parents,
                blocks=blocks,
                attributes=attributes,
                references=references,
            )
        elif item.type == "attribute":
            _index_attribute(
                item,
                source=source,
                block=_body_key(parents),
                attributes=attributes,
                references=references,
            )


def _index_block(
    node: Any,
    *,
    source: bytes,
    parents: tuple[tuple[str, tuple[str, ...]], ...],
    blocks: list[BlockSpan],
    attributes: list[AttributeSpan],
    references: list[ReferenceSpan],
) -> None:
    children = list(node.named_children)
    identifier = next(child for child in children if child.type == "identifier")
    labels = tuple(_string_text(source, child) for child in children if child.type == "string_lit")
    key = BlockKey(_node_text(source, identifier), labels, parents)
    whole = _span(node, source)
    nested_body = next((child for child in children if child.type == "body"), None)
    if nested_body is None:
        start = next(child for child in children if child.type == "block_start")
        end = next(child for child in children if child.type == "block_end")
        body = _checked_span(int(start.end_byte), int(end.start_byte), source)
    else:
        body = _span(nested_body, source)
    blocks.append(BlockSpan(key=key, whole=whole, body=body))
    if nested_body is not None:
        _index_body(
            nested_body,
            source=source,
            parents=(*parents, (key.kind, key.labels)),
            blocks=blocks,
            attributes=attributes,
            references=references,
        )


def _body_key(parents: tuple[tuple[str, tuple[str, ...]], ...]) -> BlockKey:
    if not parents:
        raise ValueError("parse failed: top-level attributes are unsupported")
    kind, labels = parents[-1]
    return BlockKey(kind=kind, labels=labels, parents=parents[:-1])


def _index_attribute(
    node: Any,
    *,
    source: bytes,
    block: BlockKey,
    attributes: list[AttributeSpan],
    references: list[ReferenceSpan],
) -> None:
    children = list(node.named_children)
    identifier = next(child for child in children if child.type == "identifier")
    expression = next((child for child in children if child.type == "expression"), None)
    if expression is None:
        raise ValueError("parse failed: attribute expression missing")
    name = _node_text(source, identifier)
    attributes.append(
        AttributeSpan(
            block=block,
            name=name,
            whole=_span(node, source),
            expression=_span(expression, source),
        )
    )
    _index_references(expression, source=source, attribute=(block, name), references=references)


def _index_references(
    node: Any,
    *,
    source: bytes,
    attribute: tuple[BlockKey, str],
    references: list[ReferenceSpan],
) -> None:
    children = list(node.named_children)
    position = 0
    while position < len(children):
        child = children[position]
        if child.type != "variable_expr":
            _index_references(child, source=source, attribute=attribute, references=references)
            position += 1
            continue
        traversal: list[str | int] = [_node_text(source, child)]
        end = child
        cursor = position + 1
        qualified = False
        while cursor < len(children) and children[cursor].type in {"get_attr", "index"}:
            part = _traversal_part(children[cursor], source)
            if part is None:
                break
            traversal.append(part)
            qualified = True
            end = children[cursor]
            cursor += 1
        if qualified:
            references.append(
                ReferenceSpan(
                    attribute=attribute,
                    traversal=tuple(traversal),
                    expression=_span_between(child, end, source),
                )
            )
            position = cursor
            continue
        _index_references(child, source=source, attribute=attribute, references=references)
        position += 1


def _traversal_part(node: Any, source: bytes) -> str | int | None:
    if node.type == "get_attr":
        identifier = next(
            (child for child in node.named_children if child.type == "identifier"), None
        )
        return None if identifier is None else _node_text(source, identifier)
    indexed = next((child for child in node.named_children if child.type == "new_index"), None)
    if indexed is None:
        return None
    expression = next(
        (child for child in indexed.named_children if child.type == "expression"), None
    )
    if expression is None:
        return None
    text = _node_text(source, expression)
    if _INTEGER.fullmatch(text):
        return int(text)
    if text.startswith('"'):
        try:
            value = json.loads(text)
        except json.JSONDecodeError:
            return None
        return value if isinstance(value, str) else None
    return None


def _string_text(source: bytes, node: Any) -> str:
    try:
        value = json.loads(_node_text(source, node))
    except json.JSONDecodeError as error:
        raise ValueError("parse failed: nonliteral block label") from error
    if not isinstance(value, str):
        raise ValueError("parse failed: non-string block label")
    return value


def _node_text(source: bytes, node: Any) -> str:
    span = _span(node, source)
    return source[span.start : span.end].decode("utf-8")


def _span(node: Any, source: bytes) -> ByteSpan:
    return _checked_span(int(node.start_byte), int(node.end_byte), source)


def _span_between(start_node: Any, end_node: Any, source: bytes) -> ByteSpan:
    return _checked_span(int(start_node.start_byte), int(end_node.end_byte), source)


def _checked_span(start: int, end: int, source: bytes) -> ByteSpan:
    if not 0 <= start <= end <= len(source):
        raise ValueError("parse failed: parser produced invalid byte span")
    return ByteSpan(start=start, end=end)
