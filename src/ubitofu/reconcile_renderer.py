# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 James Braid
"""Pure rendering of one immutable reconciliation candidate set."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import PurePosixPath

from .cleaner import VarRef
from .controller_projection import _secret_shaped
from .hcl_index import ByteSpan, index_hcl
from .hcl_patches import BytePatch, apply_patches
from .hcl_writer import render_resource, render_variable
from .import_emitter import render_import
from .module_index import IndexedResource, ModuleIndex, reindex_module
from .reconcile_model import (
    AddAttribute,
    AppendImport,
    AppendResource,
    DeclareVariable,
    DeleteResource,
    FileIdentity,
    ReconcilePlan,
    ReconcileSnapshot,
    RemoveAttribute,
    UpdateScalar,
)
from .runtime import GENERATION_SCAFFOLD_PATH
from .secrets import SECRETS, var_name
from .values import FrozenObject, FrozenValue

_GENERATED_RESOURCES = "reconciled_new"
_GENERATED_VARIABLES = "unifi-variables"
_NEW_FILE_MODE = 0o100644
_OWNERSHIP_MARKER = b"# ubitofu: reconcile-preview v1\n"


@dataclass(frozen=True)
class ProposedFile:
    relative_path: PurePosixPath
    original: FileIdentity | None
    candidate: bytes | None
    candidate_sha256: str | None
    mode: int

    def __post_init__(self) -> None:
        if self.relative_path == GENERATION_SCAFFOLD_PATH:
            raise ValueError("reserved generation scaffold destination")


@dataclass(frozen=True)
class ReconcilePreview:
    snapshot: ReconcileSnapshot
    plan: ReconcilePlan
    files: tuple[ProposedFile, ...]
    changed_paths: tuple[PurePosixPath, ...]
    candidate_digests: tuple[tuple[PurePosixPath, str | None], ...]
    valid: bool = True


def render_reconcile(
    *, snapshot: ReconcileSnapshot, plan: ReconcilePlan
) -> ReconcilePreview:
    """Render every reconciliation change in memory, or return an empty preview."""
    empty = _empty_preview(snapshot, plan)
    if plan.blocked:
        return empty
    try:
        candidates = _render_candidates(snapshot.module, snapshot.source_identities, plan)
        _validate_candidates(snapshot.module, candidates)
    except (OSError, RuntimeError, ValueError):
        return ReconcilePreview(snapshot, plan, (), (), (), False)
    originals = {source.relative_path: source.source for source in snapshot.module.sources}
    identities = {identity.relative_path: identity for identity in snapshot.source_identities}
    files: list[ProposedFile] = []
    for path, candidate in sorted(candidates.items()):
        original = originals.get(path)
        if candidate == original:
            continue
        identity = identities.get(path)
        if original is not None and identity is None:
            return ReconcilePreview(snapshot, plan, (), (), (), False)
        digest = None if candidate is None else hashlib.sha256(candidate).hexdigest()
        files.append(
            ProposedFile(
                relative_path=path,
                original=identity,
                candidate=candidate,
                candidate_sha256=digest,
                mode=_NEW_FILE_MODE if identity is None else identity.mode,
            )
        )
    changed_paths = tuple(item.relative_path for item in files)
    return ReconcilePreview(
        snapshot=snapshot,
        plan=plan,
        files=tuple(files),
        changed_paths=changed_paths,
        candidate_digests=tuple(
            (item.relative_path, item.candidate_sha256) for item in files
        ),
    )


def _empty_preview(snapshot: ReconcileSnapshot, plan: ReconcilePlan) -> ReconcilePreview:
    return ReconcilePreview(snapshot, plan, (), (), ())


def _render_candidates(
    module: ModuleIndex,
    identities: tuple[FileIdentity, ...],
    plan: ReconcilePlan,
) -> dict[PurePosixPath, bytes | None]:
    sources = {source.relative_path: source for source in module.sources}
    identity_by_path = {identity.relative_path: identity for identity in identities}
    patches: dict[PurePosixPath, list[BytePatch]] = {}
    appended_resources: dict[str, AppendResource] = {}
    appended_imports: dict[str, AppendImport] = {}
    declared_variables: set[str] = set()
    for edit in plan.edits:
        if isinstance(edit, DeclareVariable):
            if edit.name in declared_variables:
                raise ValueError("duplicate variable declaration")
            declared_variables.add(edit.name)
            continue
        if isinstance(edit, AppendResource):
            if edit.address.absolute in appended_resources:
                raise ValueError("duplicate appended resource")
            appended_resources[edit.address.absolute] = edit
            continue
        if isinstance(edit, AppendImport):
            if edit.address.absolute in appended_imports:
                raise ValueError("duplicate appended import")
            appended_imports[edit.address.absolute] = edit
            continue
        resource = _owned_native_resource(module, edit.address.absolute)
        source = sources[resource.source_path]
        identity = identity_by_path.get(resource.source_path)
        if identity is None or not _identity_matches(identity, source.source):
            raise ValueError("stale source identity")
        patches.setdefault(resource.source_path, []).append(
            _patch_for(resource, source.source, edit)
        )

    candidates: dict[PurePosixPath, bytes | None] = {}
    for path, file_patches in patches.items():
        candidates[path] = apply_patches(sources[path].source, file_patches)
    generated_resources_path = _generated_path(module, _GENERATED_RESOURCES)
    generated_variables_path = _generated_path(module, _GENERATED_VARIABLES)
    generated_resources = sources.get(generated_resources_path)
    if appended_resources or appended_imports:
        if set(appended_resources) != set(appended_imports):
            raise ValueError("append requires exactly one resource and import")
        existing_resources = {resource.address for resource in module.resources}
        existing_imports = {item.address for item in module.imports}
        if set(appended_resources).intersection(existing_resources | existing_imports):
            raise ValueError("append target is already represented in the module")
        chunks: list[bytes] = []
        variables: set[str] = set()
        for address in sorted(appended_resources):
            append = appended_resources[address]
            resource_text, names = _render_appended_resource(append)
            chunks.append(resource_text)
            variables.update(names)
            chunks.append(render_import(address, appended_imports[address].import_id).encode())
        candidates[generated_resources_path] = _append_generated(
            candidates.get(
                generated_resources_path,
                None if generated_resources is None else generated_resources.source,
            ),
            chunks,
        )
        declared_variables.update(variables)
    known_variables = {variable.name for variable in module.variables}
    missing_variables = sorted(declared_variables.difference(known_variables))
    if missing_variables:
        existing_variables = sources.get(generated_variables_path)
        candidates[generated_variables_path] = _append_generated(
            None if existing_variables is None else existing_variables.source,
            [render_variable(name).encode() for name in missing_variables],
        )
    for path in candidates:
        retained = sources.get(path)
        if retained is None:
            continue
        identity = identity_by_path.get(path)
        if identity is None or not _identity_matches(identity, retained.source):
            raise ValueError("stale generated source identity")
    return candidates


def _identity_matches(identity: FileIdentity, source: bytes) -> bool:
    return identity.size == len(source) and identity.sha256 == hashlib.sha256(source).hexdigest()


def _generated_path(module: ModuleIndex, stem: str) -> PurePosixPath:
    paths = {
        source.relative_path
        for source in module.sources
        if source.active and source.relative_path.name in {f"{stem}.tf", f"{stem}.tofu"}
    }
    if len(paths) == 1:
        return next(iter(paths))
    extension = ".tofu" if any(
        source.active and source.relative_path.name.endswith(".tofu")
        for source in module.sources
    ) else ".tf"
    return PurePosixPath(f"{stem}{extension}")


def _owned_native_resource(module: ModuleIndex, address: str) -> IndexedResource:
    matches = [
        resource
        for resource in module.resources
        if resource.address == address and resource.editable and resource.block is not None
    ]
    if len(matches) != 1:
        raise ValueError("resource is not owned by one native source")
    return matches[0]


def _patch_for(
    resource: IndexedResource,
    source: bytes,
    edit: UpdateScalar | AddAttribute | RemoveAttribute | DeleteResource,
) -> BytePatch:
    if isinstance(edit, DeleteResource):
        assert resource.block is not None
        span = resource.block.whole
        return BytePatch(span, edit.anchor.expected_literal, b"", "delete resource")
    if isinstance(edit, AddAttribute):
        assert resource.block is not None
        span = resource.block.whole
        expected = edit.anchor.expected_literal
        if source[span.start : span.end] != expected:
            raise ValueError("resource anchor does not match source")
        name = edit.attribute_path[0]
        if not isinstance(name, str) or len(edit.attribute_path) != 1:
            raise ValueError("unsupported attribute addition")
        body = resource.block.body
        body_bytes = source[body.start : body.end]
        insertion = _attribute_insertion(body_bytes, name, edit.value)
        return BytePatch(
            ByteSpan(body.end, body.end), b"", insertion, "add attribute"
        )
    if not hasattr(edit, "anchor"):
        raise ValueError("unsupported edit")
    matches = [
        attribute
        for attribute in resource.attributes
        if attribute.attribute_path == edit.anchor.attribute_path
    ]
    if len(matches) != 1:
        raise ValueError("attribute anchor is not uniquely owned")
    attribute = matches[0]
    actual = source[attribute.expression.start : attribute.expression.end]
    if actual != edit.anchor.expected_literal:
        raise ValueError("attribute anchor does not match source")
    if isinstance(edit, UpdateScalar):
        return BytePatch(
            attribute.expression, edit.anchor.expected_literal, edit.replacement, "update"
        )
    if isinstance(edit, RemoveAttribute):
        return BytePatch(
            attribute.whole,
            source[attribute.whole.start : attribute.whole.end],
            b"",
            "remove",
        )
    raise ValueError("unsupported edit")


def _attribute_insertion(body: bytes, name: str, value: bytes) -> bytes:
    prefix = b"" if body.endswith(b"\n") else b"\n"
    return prefix + b"  " + name.encode() + b" = " + value + b"\n"


def _render_appended_resource(append: AppendResource) -> tuple[bytes, set[str]]:
    attrs = _thaw_object(append.resource)
    names: set[str] = set()
    safe_attributes: set[str] = set()
    for rule in SECRETS:
        if rule.resource_type != append.address.resource_type:
            continue
        name = var_name(rule, {"name": append.address.name})
        attrs[rule.attr] = VarRef(f"var.{name}")
        names.add(name)
        safe_attributes.add(rule.attr)
    if _has_unbound_secret(append.resource, safe_attributes):
        raise ValueError("unbound secret-shaped append attribute")
    return (
        render_resource(append.address.resource_type, append.address.name, attrs).encode(),
        names,
    )


def _thaw_object(value: FrozenObject) -> dict[str, object]:
    return {key: _thaw(item) for key, item in value.items}


def _thaw(value: FrozenValue) -> object:
    if isinstance(value, FrozenObject):
        return _thaw_object(value)
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return value


def _has_unbound_secret(
    value: FrozenValue,
    safe_attributes: set[str],
) -> bool:
    if isinstance(value, FrozenObject):
        return any(
            name not in safe_attributes and _secret_shaped(name, _thaw(item))
            for name, item in value.items
        )
    if isinstance(value, tuple):
        return _secret_shaped("", _thaw(value))
    return _secret_shaped("", _thaw(value))


def _append_generated(existing: bytes | None, chunks: list[bytes]) -> bytes:
    addition = b"\n".join(chunk.rstrip(b"\n") for chunk in chunks) + b"\n"
    if existing is None:
        return _OWNERSHIP_MARKER + b"\n" + addition
    if not existing.startswith(_OWNERSHIP_MARKER):
        raise ValueError("generated destination is not ubitofu-owned")
    separator = b"" if existing.endswith(b"\n") else b"\n"
    return existing + separator + b"\n" + addition


def _validate_candidates(
    module: ModuleIndex, candidates: dict[PurePosixPath, bytes | None]
) -> None:
    for path, source in candidates.items():
        if source is not None and path.name.endswith((".tf", ".tofu")):
            index_hcl(path=path, source=source)
    reindex_module(module, tuple(sorted(candidates.items())))
