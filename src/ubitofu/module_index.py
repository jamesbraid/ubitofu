# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 James Braid
"""One read-only view of the effective OpenTofu module sources."""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Literal

from .hcl_index import BlockSpan, ByteSpan, HclIndex, index_hcl
from .values import FrozenObject, FrozenValue, freeze_value


@dataclass(frozen=True)
class IndexedSource:
    """One discovered source, including inactive compatibility sources."""

    relative_path: PurePosixPath
    syntax: Literal["native", "json"]
    active: bool
    hcl: HclIndex | None
    json_value: FrozenValue | None
    source: bytes = b""


@dataclass(frozen=True)
class IndexedAttribute:
    """One native-HCL resource attribute and its exact source spans."""

    attribute_path: tuple[str | int, ...]
    whole: ByteSpan
    expression: ByteSpan
    literal: bool


@dataclass(frozen=True)
class IndexedResource:
    """An effective resource address and the source that currently owns it."""

    address: str
    source_path: PurePosixPath
    block: BlockSpan | None
    editable: bool
    attributes: tuple[IndexedAttribute, ...] = ()


@dataclass(frozen=True)
class IndexedImport:
    """An import declaration visible in the effective module."""

    address: str
    import_id: str
    source_path: PurePosixPath
    block: BlockSpan | None = None
    editable: bool = False


@dataclass(frozen=True)
class IndexedVariable:
    """A module input variable declaration."""

    name: str
    source_path: PurePosixPath


@dataclass(frozen=True)
class IndexedReference:
    """A qualified native-HCL traversal and its original expression extent."""

    source_path: PurePosixPath
    target_address: str
    expression: ByteSpan | None
    kind: Literal["ordinary", "import_to"] = "ordinary"


@dataclass(frozen=True)
class ModuleIndex:
    """Immutable structural facts for the effective root-module configuration."""

    sources: tuple[IndexedSource, ...]
    resources: tuple[IndexedResource, ...]
    imports: tuple[IndexedImport, ...]
    variables: tuple[IndexedVariable, ...]
    references: tuple[IndexedReference, ...]


@dataclass(frozen=True)
class _Candidate:
    path: PurePosixPath
    source: bytes
    syntax: Literal["native", "json"]
    tofu: bool
    base_name: str
    override: bool


_JSON_IDENTIFIER = r"[A-Za-z_][A-Za-z0-9_-]*"
_JSON_ADDRESS = re.compile(
    rf"{_JSON_IDENTIFIER}(?:\.{_JSON_IDENTIFIER}|\[(?:0|[1-9][0-9]*)\]|\[\"(?:[^\"\\]|\\.)*\"\])+"
)
_JSON_ONE_LABEL_BLOCKS = frozenset({"variable", "output", "module", "provider", "check"})
_JSON_TWO_LABEL_BLOCKS = frozenset({"resource", "data"})
_JSON_BODY_BLOCKS = frozenset({"terraform", "locals"})
_JSON_UNLABELED_BLOCKS = frozenset({"moved", "removed"})
_JSON_TOP_LEVEL_BLOCKS = (
    _JSON_ONE_LABEL_BLOCKS
    | _JSON_TWO_LABEL_BLOCKS
    | _JSON_BODY_BLOCKS
    | _JSON_UNLABELED_BLOCKS
    | {"import", "//"}
)


def index_effective_module(
    *,
    workdir: Path,
    candidates: tuple[tuple[PurePosixPath, bytes | None], ...] = (),
) -> ModuleIndex:
    """Index the active OpenTofu sources after applying an in-memory candidate overlay."""
    files = {
        PurePosixPath(path.name): path.read_bytes() for path in workdir.iterdir() if path.is_file()
    }
    return _index_sources(_apply_candidates(files, candidates))


def reindex_module(
    module: ModuleIndex,
    candidates: tuple[tuple[PurePosixPath, bytes | None], ...] = (),
) -> ModuleIndex:
    """Reindex retained module bytes after applying an in-memory candidate overlay."""
    files = {source.relative_path: source.source for source in module.sources}
    return _index_sources(_apply_candidates(files, candidates))


def _index_sources(files: dict[PurePosixPath, bytes]) -> ModuleIndex:
    candidates_by_path = {
        path: candidate
        for path, source in files.items()
        if (candidate := _candidate(path, source)) is not None
    }
    active_paths = _active_paths(candidates_by_path.values())
    indexed_sources: list[IndexedSource] = []
    parsed: dict[PurePosixPath, HclIndex | FrozenValue] = {}
    for path, candidate in sorted(candidates_by_path.items()):
        active = path in active_paths
        value: HclIndex | FrozenValue | None = None
        if active:
            if candidate.syntax == "native":
                value = index_hcl(path=path, source=candidate.source)
            else:
                value = _decode_json(candidate.source)
            parsed[path] = value
        indexed_sources.append(
            IndexedSource(
                relative_path=path,
                syntax=candidate.syntax,
                active=active,
                hcl=value if isinstance(value, HclIndex) else None,
                json_value=None if isinstance(value, HclIndex) else value,
                source=candidate.source,
            )
        )

    resources: dict[str, IndexedResource] = {}
    imports: list[IndexedImport] = []
    variables: list[IndexedVariable] = []
    references: list[IndexedReference] = []
    normal = sorted(
        (
            candidate
            for candidate in candidates_by_path.values()
            if candidate.path in active_paths and not candidate.override
        ),
        key=lambda candidate: candidate.path,
    )
    overrides = sorted(
        (
            candidate
            for candidate in candidates_by_path.values()
            if candidate.path in active_paths and candidate.override
        ),
        key=lambda candidate: candidate.path,
    )
    for candidate in normal:
        _collect_candidate(
            candidate,
            parsed[candidate.path],
            resources=resources,
            imports=imports,
            variables=variables,
            references=references,
            overriding=False,
        )
    for candidate in overrides:
        _collect_candidate(
            candidate,
            parsed[candidate.path],
            resources=resources,
            imports=imports,
            variables=variables,
            references=references,
            overriding=True,
        )
    return ModuleIndex(
        sources=tuple(indexed_sources),
        resources=tuple(sorted(resources.values(), key=lambda item: item.address)),
        imports=tuple(sorted(imports, key=lambda item: (item.address, item.source_path))),
        variables=tuple(sorted(variables, key=lambda item: (item.name, item.source_path))),
        references=tuple(references),
    )


def _apply_candidates(
    files: dict[PurePosixPath, bytes],
    candidates: tuple[tuple[PurePosixPath, bytes | None], ...],
) -> dict[PurePosixPath, bytes]:
    updated = dict(files)
    for path, source in candidates:
        if path.is_absolute() or path.parent != PurePosixPath("."):
            raise ValueError("candidate path must be a module-relative filename")
        if source is None:
            updated.pop(path, None)
        else:
            updated[path] = source
    return updated


def _candidate(path: PurePosixPath, source: bytes) -> _Candidate | None:
    name = path.name
    suffixes: tuple[tuple[str, Literal["native", "json"], bool], ...] = (
        (".tofu.json", "json", True),
        (".tf.json", "json", False),
        (".tofu", "native", True),
        (".tf", "native", False),
    )
    for suffix, syntax, tofu in suffixes:
        if name.endswith(suffix):
            base_name = name[: -len(suffix)]
            return _Candidate(
                path=path,
                source=source,
                syntax=syntax,
                tofu=tofu,
                base_name=base_name,
                override=base_name == "override" or base_name.endswith("_override"),
            )
    return None


def _active_paths(candidates: Iterable[_Candidate]) -> frozenset[PurePosixPath]:
    candidate_tuple = tuple(candidates)
    tofu_keys = {(item.base_name, item.syntax) for item in candidate_tuple if item.tofu}
    return frozenset(
        item.path
        for item in candidate_tuple
        if item.tofu or (item.base_name, item.syntax) not in tofu_keys
    )


def _decode_json(source: bytes) -> FrozenValue:
    def pairs(values: list[tuple[str, object]]) -> dict[str, object]:
        object_value: dict[str, object] = {}
        for key, value in values:
            if key in object_value:
                raise ValueError("duplicate JSON key")
            object_value[key] = value
        return object_value

    def invalid_constant(_: str) -> object:
        raise ValueError("invalid JSON constant")

    try:
        value = json.loads(
            source.decode("utf-8", errors="strict"),
            object_pairs_hook=pairs,
            parse_constant=invalid_constant,
        )
        return freeze_value(value)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise ValueError("invalid JSON source") from error


def _collect_candidate(
    candidate: _Candidate,
    value: HclIndex | FrozenValue,
    *,
    resources: dict[str, IndexedResource],
    imports: list[IndexedImport],
    variables: list[IndexedVariable],
    references: list[IndexedReference],
    overriding: bool,
) -> None:
    if isinstance(value, HclIndex):
        _collect_native(
            candidate,
            value,
            resources=resources,
            imports=imports,
            variables=variables,
            references=references,
            overriding=overriding,
        )
    else:
        _collect_json(
            candidate,
            value,
            resources=resources,
            imports=imports,
            variables=variables,
            references=references,
            overriding=overriding,
        )


def _collect_native(
    candidate: _Candidate,
    index: HclIndex,
    *,
    resources: dict[str, IndexedResource],
    imports: list[IndexedImport],
    variables: list[IndexedVariable],
    references: list[IndexedReference],
    overriding: bool,
) -> None:
    import_to_spans: set[tuple[int, int]] = set()
    for block in index.blocks:
        if block.key.parents:
            continue
        if block.key.kind == "resource" and len(block.key.labels) == 2:
            _add_resource(
                resources,
                IndexedResource(
                    address=".".join(block.key.labels),
                    source_path=candidate.path,
                    block=block,
                    editable=True,
                    attributes=tuple(
                        IndexedAttribute(
                            (attribute.name,),
                            attribute.whole,
                            attribute.expression,
                            _is_literal_expression(
                                candidate.source[
                                    attribute.expression.start : attribute.expression.end
                                ]
                            ),
                        )
                        for attribute in index.attributes
                        if attribute.block == block.key
                    ),
                ),
                overriding=overriding,
            )
        elif block.key.kind == "variable" and len(block.key.labels) == 1:
            variables.append(IndexedVariable(name=block.key.labels[0], source_path=candidate.path))
        elif block.key.kind == "import" and not block.key.labels:
            expression = _collect_native_import(
                candidate.path, candidate.source, index, block, imports
            )
            if expression is not None:
                import_to_spans.add((expression.start, expression.end))
    for reference in index.references:
        references.append(
            IndexedReference(
                source_path=candidate.path,
                target_address=_address(reference.traversal),
                expression=reference.expression,
                kind=(
                    "import_to"
                    if (reference.expression.start, reference.expression.end)
                    in import_to_spans
                    else "ordinary"
                ),
            )
        )


def _collect_native_import(
    source_path: PurePosixPath,
    source: bytes,
    index: HclIndex,
    block: BlockSpan,
    imports: list[IndexedImport],
) -> ByteSpan | None:
    attributes = [
        attribute
        for attribute in index.attributes
        if attribute.block == block.key and _within(attribute.whole, block.whole)
    ]
    to_attributes = [attribute for attribute in attributes if attribute.name == "to"]
    import_ids = [attribute for attribute in attributes if attribute.name == "id"]
    to_references = [
        reference
        for reference in index.references
        if reference.attribute == (block.key, "to") and _within(reference.expression, block.whole)
    ]
    if len(to_attributes) != 1 or len(import_ids) != 1 or len(to_references) != 1:
        return None
    raw_id = source[import_ids[0].expression.start : import_ids[0].expression.end].decode("utf-8")
    try:
        value = json.loads(raw_id)
    except json.JSONDecodeError:
        return None
    if isinstance(value, str):
        imports.append(
            IndexedImport(
                address=_address(to_references[0].traversal),
                import_id=value,
                source_path=source_path,
                block=block,
                editable=True,
            )
        )
        return to_references[0].expression
    return None


def _within(inner: ByteSpan, outer: ByteSpan) -> bool:
    return outer.start <= inner.start and inner.end <= outer.end


def _is_literal_expression(source: bytes) -> bool:
    try:
        value = json.loads(source)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return False
    return value is None or isinstance(value, str | int | float | bool)


def _collect_json(
    candidate: _Candidate,
    value: FrozenValue,
    *,
    resources: dict[str, IndexedResource],
    imports: list[IndexedImport],
    variables: list[IndexedVariable],
    references: list[IndexedReference],
    overriding: bool,
) -> None:
    root = _json_object(value, "root")
    _validate_json_configuration(root)
    imports_value = root.get("import")
    if imports_value is not None:
        _collect_json_imports(candidate.path, imports_value, imports, references)
    resource_types = root.get("resource")
    if resource_types is not None:
        for resource_type, names in _json_object(resource_types, "resource").items():
            for name, body in _json_object(names, f"resource.{resource_type}").items():
                _json_object(body, f"resource.{resource_type}.{name}")
                _add_resource(
                    resources,
                    IndexedResource(
                        address=f"{resource_type}.{name}",
                        source_path=candidate.path,
                        block=None,
                        editable=False,
                    ),
                    overriding=overriding,
                )
    variables_value = root.get("variable")
    if variables_value is not None:
        for name, body in _json_object(variables_value, "variable").items():
            _json_object(body, f"variable.{name}")
            variables.append(IndexedVariable(name=name, source_path=candidate.path))
    for name, section in root.items():
        if name not in {"//", "import"}:
            references.extend(_json_references(candidate.path, section))


def _collect_json_imports(
    source_path: PurePosixPath,
    value: FrozenValue,
    imports: list[IndexedImport],
    references: list[IndexedReference],
) -> None:
    if not isinstance(value, tuple):
        raise ValueError("invalid JSON import section")
    for ordinal, item in enumerate(value):
        block = _json_object(item, f"import[{ordinal}]")
        fields = set(block).difference({"//"})
        if fields != {"to", "id"}:
            raise ValueError("invalid JSON import fields")
        address = _json_static_address(block["to"], "import to")
        import_id = _json_string(block["id"], "import id")
        imports.append(IndexedImport(address=address, import_id=import_id, source_path=source_path))
        references.append(
            IndexedReference(
                source_path=source_path,
                target_address=address,
                expression=None,
                kind="import_to",
            )
        )
        for reference in _json_template_references(import_id):
            references.append(
                IndexedReference(source_path=source_path, target_address=reference, expression=None)
            )


def _validate_json_configuration(root: Mapping[str, FrozenValue]) -> None:
    for name, value in root.items():
        if name not in _JSON_TOP_LEVEL_BLOCKS:
            raise ValueError(f"invalid JSON top-level section: {name}")
        if name in _JSON_TWO_LABEL_BLOCKS:
            _validate_json_labeled_section(value, name, labels=2)
        elif name in _JSON_ONE_LABEL_BLOCKS:
            _validate_json_labeled_section(value, name, labels=1)
        elif name in _JSON_BODY_BLOCKS:
            _json_object(value, name)
        elif name in _JSON_UNLABELED_BLOCKS:
            _validate_json_unlabeled_section(value, name)


def _validate_json_labeled_section(value: FrozenValue, name: str, *, labels: int) -> None:
    entries: Mapping[str, FrozenValue] = _json_object(value, name)
    for depth in range(labels):
        next_entries: dict[str, FrozenValue] = {}
        for label, child in entries.items():
            object_child = _json_object(child, f"{name} label {label}")
            if depth == labels - 1:
                continue
            next_entries.update(object_child)
        entries = next_entries


def _validate_json_unlabeled_section(value: FrozenValue, name: str) -> None:
    if not isinstance(value, tuple):
        raise ValueError(f"invalid JSON {name} section")
    for ordinal, body in enumerate(value):
        _json_object(body, f"{name}[{ordinal}]")


def _json_object(value: FrozenValue, context: str) -> Mapping[str, FrozenValue]:
    if not isinstance(value, FrozenObject):
        raise ValueError(f"invalid JSON {context} shape")
    return dict(value.items)


def _json_string(value: FrozenValue, context: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"invalid JSON {context} value")
    return value


def _json_static_address(value: FrozenValue, context: str) -> str:
    address = _json_string(value, context)
    if not _JSON_ADDRESS.fullmatch(address):
        raise ValueError(f"invalid JSON {context} address")
    return address


def _json_references(source_path: PurePosixPath, value: FrozenValue) -> list[IndexedReference]:
    if isinstance(value, str):
        return [
            IndexedReference(source_path=source_path, target_address=target, expression=None)
            for target in _json_template_references(value)
        ]
    if isinstance(value, tuple):
        return [reference for item in value for reference in _json_references(source_path, item)]
    if isinstance(value, FrozenObject):
        return [
            reference
            for name, item in value.items
            if name != "//"
            for reference in _json_references(source_path, item)
        ]
    return []


def _json_template_references(value: str) -> tuple[str, ...]:
    targets: list[str] = []
    offset = 0
    while offset < len(value):
        if value.startswith("$${", offset):
            offset += 3
            continue
        if not value.startswith("${", offset):
            offset += 1
            continue
        end = value.find("}", offset + 2)
        if end < 0:
            raise ValueError("invalid JSON ambiguous reference")
        target = value[offset + 2 : end]
        if not _JSON_ADDRESS.fullmatch(target):
            raise ValueError("invalid JSON ambiguous reference")
        targets.append(target)
        offset = end + 1
    return tuple(targets)


def _add_resource(
    resources: dict[str, IndexedResource], resource: IndexedResource, *, overriding: bool
) -> None:
    if resource.address in resources and not overriding:
        raise ValueError(f"duplicate resource address: {resource.address}")
    if overriding and resource.address not in resources:
        raise ValueError(f"override resource has no base: {resource.address}")
    resources[resource.address] = resource


def _address(traversal: tuple[str | int, ...]) -> str:
    result = ""
    for part in traversal:
        if isinstance(part, int):
            result += f"[{part}]"
        elif result:
            result += f".{part}"
        else:
            result = part
    return result
