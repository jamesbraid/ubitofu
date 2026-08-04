# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 James Braid
"""Pure construction of immutable three-way reconciliation observations."""

from __future__ import annotations

import hashlib
from dataclasses import replace
from pathlib import Path, PurePosixPath

from .controller import Controller
from .controller_projection import build_controller_snapshot, project_controller_snapshot
from .enumerator import enumerate_controller
from .manifest import spec_for_type
from .module_index import IndexedSource, ModuleIndex
from .reconcile_model import (
    CollectionIdentityPolicy,
    ControllerProjection,
    FileIdentity,
    LifecyclePolicy,
    OpenTofuAddress,
    PlanDocument,
    ProviderSchema,
    ReconcileSnapshot,
    ResourceChange,
    ResourceObservation,
    SecretChangeFact,
    SecretChangeKind,
    SourceAttribute,
    SourceResource,
    parse_opentofu_address,
)
from .tofu_json import parse_plan_document, parse_provider_schema
from .tofu_runner import TofuRunner
from .values import FrozenObject, FrozenValue, freeze_value


def normalize_reconcile_snapshot(
    *, plan: PlanDocument, schema: ProviderSchema, live: ControllerProjection, module: ModuleIndex
) -> ReconcileSnapshot:
    """Build observations without I/O or substituting current backend state."""
    schema_by_type = dict(schema.resources)
    base = dict(plan.prior_state.resources)
    state_sensitive = dict(plan.prior_state.sensitive_values)
    changes = {change.address: change for change in plan.changes}
    plan_time_live = dict(plan.plan_time_live)
    projected = {resource.address: resource for resource in live.resources}
    committed = _committed_resources(module, base, changes)
    addresses = set(base) | set(changes) | set(plan_time_live) | set(projected) | set(committed)
    observations: list[ResourceObservation] = []
    for address in sorted(addresses):
        change = changes.get(address)
        desired = change.after if change is not None else None
        base_value = base.get(address)
        live_value = plan_time_live.get(address)
        collection_identities: tuple[CollectionIdentityPolicy, ...]
        spec = None
        try:
            spec = spec_for_type(address.resource_type)
        except KeyError:
            lifecycle = LifecyclePolicy(False, "attention")
            collection_identities = ()
        else:
            lifecycle = spec.lifecycle
            collection_identities = spec.collection_identities
        projection = projected.get(address)
        secret_changes: tuple[SecretChangeFact, ...] = ()
        secret_paths: set[tuple[str | int, ...]] = set()
        write_only_paths: set[tuple[str | int, ...]] = set()
        dynamic_collection_paths: set[tuple[str | int, ...]] = set()
        resource_schema = schema_by_type.get(address.resource_type)
        if resource_schema is not None:
            secret_paths, write_only_paths, dynamic_collection_paths = _schema_secret_paths(
                resource_schema, base_value, desired, live_value
            )
        if change is not None:
            secret_paths.update(_truthy_paths(change.before_sensitive))
            secret_paths.update(_truthy_paths(change.after_sensitive))
        retained_sensitive = state_sensitive.get(address)
        if retained_sensitive is not None:
            secret_paths.update(_truthy_paths(retained_sensitive))
        secret_changes = tuple(
            SecretChangeFact(
                _public_secret_path(path, dynamic_collection_paths),
                _classify_secret_path(base_value, desired, live_value, path),
                path not in write_only_paths,
            )
            for path in sorted(secret_paths, key=repr)
        )
        if projection is not None:
            selected_paths = projection.comparable_paths
            base_value = _select_paths(base_value, selected_paths)
            desired = _select_paths(desired, selected_paths)
            live_value = _select_paths(live_value, selected_paths)
        if secret_paths:
            base_value = _without_optional_paths(base_value, secret_paths)
            desired = _without_optional_paths(desired, secret_paths)
            live_value = _without_optional_paths(live_value, secret_paths)
        sanitized_change = _sanitize_change(change, secret_paths)
        committed_source = committed.get(address)
        if committed_source is not None and secret_paths:
            committed_source = replace(
                committed_source,
                attributes=_without_paths(committed_source.attributes, secret_paths),
            )
        blockers = set(live.blocking_reasons)
        if projection is not None:
            blockers.update(projection.blocking_reasons)
        observations.append(
            ResourceObservation(
                address=address,
                committed=committed_source,
                base=base_value,
                desired=desired,
                live=live_value,
                change=sanitized_change,
                lifecycle=lifecycle,
                collection_identities=collection_identities,
                blocking_reasons=tuple(sorted(blockers, key=lambda item: item.value)),
                fresh_present=None if projection is None else projection.present,
                fresh=(
                    None
                    if projection is None or projection.values is None
                    else _without_paths(projection.values, secret_paths)
                ),
                import_id=None if projection is None else projection.import_id,
                secret_changes=secret_changes,
            )
        )
    source_identities = tuple(
        sorted(
            {
                resource.file.relative_path: resource.file for resource in committed.values()
            }.values(),
            key=lambda item: item.relative_path,
        )
    )
    return ReconcileSnapshot(
        resources=tuple(observations),
        module=module,
        source_identities=source_identities,
        controller_digest=live.canonical_sha256,
    )


def collect_reconcile_snapshot(
    *, controller: Controller, runner: TofuRunner, module: ModuleIndex
) -> ReconcileSnapshot:
    """Collect one read-only plan/controller/source snapshot before any persistent write."""
    plan_path = runner.plan_path
    if plan_path is None:
        raise ValueError("reconciliation plan path is not runtime-owned")
    runner.plan(out=plan_path)
    plan = parse_plan_document(runner.show_json(plan_path))
    schema = parse_provider_schema(runner.providers_schema())
    enumeration = enumerate_controller(controller, capture_records=True)
    raw_records: list[tuple[str, str, dict[str, object]]] = []
    for record in enumeration.records:
        raw = _thaw_object(record.raw)
        raw_records.append((record.resource_type, record.import_id, raw))
    controller_snapshot = build_controller_snapshot(
        records=tuple(raw_records),
        covered_resource_types=tuple(enumeration.covered_resource_types),
        name_hints={
            (target.resource_type, target.import_id): target.name_hint
            for target in enumeration.targets
        },
    )
    projection = project_controller_snapshot(
        plan=plan,
        controller=controller_snapshot,
        schema=schema,
    )
    normalized = normalize_reconcile_snapshot(
        plan=plan,
        schema=schema,
        live=projection,
        module=module,
    )
    return _capture_source_files(normalized, runner.workdir)


def _committed_resources(
    module: ModuleIndex,
    base: dict[OpenTofuAddress, FrozenObject],
    changes: dict[OpenTofuAddress, ResourceChange],
) -> dict[OpenTofuAddress, SourceResource]:
    sources = {source.relative_path: source for source in module.sources}
    by_absolute: dict[str, OpenTofuAddress] = {}
    for address in set(base) | set(changes):
        if address.deposed is None:
            by_absolute[address.absolute] = address
    committed: dict[OpenTofuAddress, SourceResource] = {}
    for indexed in module.resources:
        try:
            address = by_absolute.get(indexed.address) or parse_opentofu_address(indexed.address)
        except ValueError:
            continue
        change = changes.get(address)
        attributes = (change.after if change is not None else None) or base.get(address)
        if attributes is None:
            attributes = FrozenObject(())
        source = sources.get(indexed.source_path)
        identity = _structural_identity(indexed.source_path, source)
        committed[address] = SourceResource(address, identity, b"", attributes)
    return committed


def _structural_identity(path: PurePosixPath, source: IndexedSource | None) -> FileIdentity:
    if source is not None and source.hcl is not None:
        sha256 = source.hcl.source_sha256
    elif source is not None and source.json_value is not None:
        sha256 = hashlib.sha256(repr(source.json_value).encode()).hexdigest()
    else:
        sha256 = hashlib.sha256(b"").hexdigest()
    return FileIdentity(path, 0, 0, 0, 0, 0, 0, 0, sha256)


def _select_paths(
    value: FrozenObject | None, paths: tuple[tuple[str | int, ...], ...]
) -> FrozenObject | None:
    if value is None:
        return None
    tree: dict[str | int, object] = {}
    for path in paths:
        _add_path(tree, path)
    selected = _prune_value(value, tree)
    if not isinstance(selected, FrozenObject):
        raise ValueError("selected provider paths did not retain an object")
    return selected


def _without_paths(
    value: FrozenObject, paths: set[tuple[str | int, ...]]
) -> FrozenObject:
    mutable = _thaw_object(value)
    for path in paths:
        _remove_path(mutable, path)
    frozen = freeze_value(mutable)
    assert isinstance(frozen, FrozenObject)
    return frozen


def _without_optional_paths(
    value: FrozenObject | None, paths: set[tuple[str | int, ...]]
) -> FrozenObject | None:
    return None if value is None else _without_paths(value, paths)


def _sanitize_change(
    change: ResourceChange | None,
    secret_paths: set[tuple[str | int, ...]],
) -> ResourceChange | None:
    if change is None or not secret_paths:
        return change
    before = None if change.before is None else _without_paths(change.before, secret_paths)
    after = None if change.after is None else _without_paths(change.after, secret_paths)
    return replace(change, before=before, after=after)


def _classify_secret_path(
    base: FrozenObject | None,
    desired: FrozenObject | None,
    live: FrozenObject | None,
    path: tuple[str | int, ...],
) -> SecretChangeKind:
    base_value = _path_or_absent(base, path)
    desired_value = _path_or_absent(desired, path)
    live_value = _path_or_absent(live, path)
    if base_value == desired_value == live_value:
        return SecretChangeKind.UNCHANGED
    if base_value == live_value and desired_value != base_value:
        return SecretChangeKind.CODE_ONLY
    if base_value == desired_value and live_value != base_value:
        return SecretChangeKind.LIVE_ONLY
    if desired_value == live_value and base_value != desired_value:
        return SecretChangeKind.CONVERGED
    return SecretChangeKind.CONFLICT


def _path_or_absent(
    value: FrozenObject | None, path: tuple[str | int, ...]
) -> FrozenValue | object:
    if value is None:
        return _MISSING
    return _frozen_path(value, path)


def _truthy_paths(
    value: FrozenValue, prefix: tuple[str | int, ...] = ()
) -> set[tuple[str | int, ...]]:
    if isinstance(value, FrozenObject):
        return {
            path
            for name, item in value.items
            for path in _truthy_paths(item, (*prefix, name))
        }
    if isinstance(value, tuple):
        return {
            path
            for index, item in enumerate(value)
            for path in _truthy_paths(item, (*prefix, index))
        }
    return {prefix} if value is True else set()


def _schema_secret_paths(
    schema: FrozenObject,
    *values: FrozenObject | None,
) -> tuple[
    set[tuple[str | int, ...]],
    set[tuple[str | int, ...]],
    set[tuple[str | int, ...]],
]:
    schema_value = _thaw_object(schema)
    block = schema_value.get("block")
    if not isinstance(block, dict):
        return set(), set(), set()
    secret: set[tuple[str | int, ...]] = set()
    write_only: set[tuple[str | int, ...]] = set()
    dynamic_collections: set[tuple[str | int, ...]] = set()
    _collect_schema_secret_paths(
        block,
        tuple(value for value in values if value is not None),
        (),
        secret,
        write_only,
        dynamic_collections,
    )
    return secret, write_only, dynamic_collections


def _collect_schema_secret_paths(
    block: dict[str, object],
    values: tuple[FrozenValue, ...],
    prefix: tuple[str | int, ...],
    secret: set[tuple[str | int, ...]],
    write_only: set[tuple[str | int, ...]],
    dynamic_collections: set[tuple[str | int, ...]],
) -> None:
    attributes = block.get("attributes", {})
    if isinstance(attributes, dict):
        for name, raw_schema in attributes.items():
            if not isinstance(name, str) or not isinstance(raw_schema, dict):
                continue
            path = (*prefix, name)
            if raw_schema.get("sensitive") or raw_schema.get("write_only"):
                secret.add(path)
                if raw_schema.get("write_only"):
                    write_only.add(path)
            attribute_values = tuple(
                child
                for value in values
                for child in (_frozen_path(value, (name,)),)
                if child is not _MISSING
            )
            _collect_cty_dynamic_paths(
                raw_schema.get("type"),
                attribute_values,
                path,
                dynamic_collections,
            )
            nested = raw_schema.get("nested_type")
            if not isinstance(nested, dict):
                continue
            nested_attrs = nested.get("attributes")
            if not isinstance(nested_attrs, dict):
                continue
            _collect_nested_secret_paths(
                name,
                {"attributes": nested_attrs},
                str(nested.get("nesting_mode", "")),
                values,
                prefix,
                secret,
                write_only,
                dynamic_collections,
            )
    block_types = block.get("block_types", {})
    if isinstance(block_types, dict):
        for name, raw_schema in block_types.items():
            if not isinstance(name, str) or not isinstance(raw_schema, dict):
                continue
            nested_block = raw_schema.get("block")
            if not isinstance(nested_block, dict):
                continue
            _collect_nested_secret_paths(
                name,
                nested_block,
                str(raw_schema.get("nesting_mode", "")),
                values,
                prefix,
                secret,
                write_only,
                dynamic_collections,
            )


def _collect_nested_secret_paths(
    name: str,
    nested_block: dict[str, object],
    mode: str,
    values: tuple[FrozenValue, ...],
    prefix: tuple[str | int, ...],
    secret: set[tuple[str | int, ...]],
    write_only: set[tuple[str | int, ...]],
    dynamic_collections: set[tuple[str | int, ...]],
) -> None:
    path = (*prefix, name)
    children = tuple(_frozen_path(value, (name,)) for value in values)
    if mode == "single":
        nested_values = tuple(item for item in children if isinstance(item, FrozenObject))
        _collect_schema_secret_paths(
            nested_block,
            nested_values,
            path,
            secret,
            write_only,
            dynamic_collections,
        )
        return
    if mode == "map":
        dynamic_collections.add(path)
        keys = {
            key
            for child in children
            if isinstance(child, FrozenObject)
            for key, _ in child.items
        }
        for key in sorted(keys):
            nested_values = tuple(
                item
                for child in children
                if isinstance(child, FrozenObject)
                for item in (_frozen_path(child, (key,)),)
                if isinstance(item, FrozenObject)
            )
            _collect_schema_secret_paths(
                nested_block,
                nested_values,
                (*path, key),
                secret,
                write_only,
                dynamic_collections,
            )
        return
    if mode == "set":
        dynamic_collections.add(path)
    indexes = {
        index
        for child in children
        if isinstance(child, tuple)
        for index in range(len(child))
    }
    for index in sorted(indexes):
        nested_values = tuple(
            child[index]
            for child in children
            if isinstance(child, tuple)
            and index < len(child)
            and isinstance(child[index], FrozenObject)
        )
        _collect_schema_secret_paths(
            nested_block,
            nested_values,
            (*path, index),
            secret,
            write_only,
            dynamic_collections,
        )


def _public_secret_path(
    path: tuple[str | int, ...],
    dynamic_collections: set[tuple[str | int, ...]],
) -> tuple[str | int, ...]:
    public = list(path)
    for collection_path in sorted(dynamic_collections, key=len):
        if path[: len(collection_path)] == collection_path and len(path) > len(
            collection_path
        ):
            public[len(collection_path)] = "*"
    return tuple(public)


def _collect_cty_dynamic_paths(
    type_expression: object,
    values: tuple[object, ...],
    path: tuple[str | int, ...],
    dynamic_collections: set[tuple[str | int, ...]],
) -> None:
    """Record only the runtime-identity positions in one cty type expression."""
    if not isinstance(type_expression, list) or len(type_expression) != 2:
        return
    kind, element_type = type_expression
    if kind == "map":
        dynamic_collections.add(path)
        keys = {
            key
            for value in values
            if isinstance(value, FrozenObject)
            for key, _ in value.items
        }
        for key in sorted(keys):
            children = tuple(
                child
                for value in values
                if isinstance(value, FrozenObject)
                for child in (_frozen_path(value, (key,)),)
                if child is not _MISSING
            )
            _collect_cty_dynamic_paths(
                element_type, children, (*path, key), dynamic_collections
            )
        return
    if kind in {"list", "set"}:
        if kind == "set":
            dynamic_collections.add(path)
        indexes = {
            index
            for value in values
            if isinstance(value, tuple)
            for index in range(len(value))
        }
        for index in sorted(indexes):
            children = tuple(
                value[index]
                for value in values
                if isinstance(value, tuple) and index < len(value)
            )
            _collect_cty_dynamic_paths(
                element_type, children, (*path, index), dynamic_collections
            )
        return
    if kind == "tuple" and isinstance(element_type, list):
        for index, child_type in enumerate(element_type):
            children = tuple(
                value[index]
                for value in values
                if isinstance(value, tuple) and index < len(value)
            )
            _collect_cty_dynamic_paths(
                child_type, children, (*path, index), dynamic_collections
            )
        return
    if kind == "object" and isinstance(element_type, dict):
        for name, child_type in element_type.items():
            if not isinstance(name, str):
                continue
            children = tuple(
                child
                for value in values
                if isinstance(value, FrozenObject)
                for child in (_frozen_path(value, (name,)),)
                if child is not _MISSING
            )
            _collect_cty_dynamic_paths(
                child_type, children, (*path, name), dynamic_collections
            )


def _remove_path(value: object, path: tuple[str | int, ...]) -> None:
    if not path:
        return
    current = value
    for part in path[:-1]:
        if isinstance(part, str) and isinstance(current, dict):
            current = current.get(part)
        elif isinstance(part, int) and isinstance(current, list) and 0 <= part < len(current):
            current = current[part]
        else:
            return
    last = path[-1]
    if isinstance(last, str) and isinstance(current, dict):
        current.pop(last, None)
    elif isinstance(last, int) and isinstance(current, list) and 0 <= last < len(current):
        current[last] = None


_MISSING = object()


def _frozen_path(value: FrozenValue, path: tuple[str | int, ...]) -> FrozenValue | object:
    current: FrozenValue = value
    for part in path:
        if isinstance(part, str) and isinstance(current, FrozenObject):
            fields = dict(current.items)
            if part not in fields:
                return _MISSING
            current = fields[part]
        elif isinstance(part, int) and isinstance(current, tuple) and 0 <= part < len(current):
            current = current[part]
        else:
            return _MISSING
    return current


_TERMINAL = object()


def _add_path(tree: dict[str | int, object], path: tuple[str | int, ...]) -> None:
    current = tree
    for part in path:
        child = current.setdefault(part, {})
        if not isinstance(child, dict):
            return
        current = child
    current[_TERMINAL] = _TERMINAL  # type: ignore[index]


def _prune_value(value: FrozenValue, tree: dict[str | int, object]) -> FrozenValue:
    if _TERMINAL in tree:
        return value
    if isinstance(value, FrozenObject):
        fields = dict(value.items)
        selected: list[tuple[str, FrozenValue]] = []
        for part, child in tree.items():
            if isinstance(part, str) and part in fields and isinstance(child, dict):
                selected.append((part, _prune_value(fields[part], child)))
        return FrozenObject(tuple(sorted(selected)))
    if isinstance(value, tuple):
        selected_items: list[FrozenValue] = []
        for part, child in sorted(tree.items(), key=lambda item: repr(item[0])):
            if isinstance(part, int) and 0 <= part < len(value) and isinstance(child, dict):
                selected_items.append(_prune_value(value[part], child))
        return tuple(selected_items)
    return value


def _capture_source_files(snapshot: ReconcileSnapshot, workdir: Path) -> ReconcileSnapshot:
    identities: dict[PurePosixPath, FileIdentity] = {}
    source_bytes: dict[PurePosixPath, bytes] = {}
    for source in snapshot.module.sources:
        if not source.active:
            continue
        path = workdir / source.relative_path
        raw = path.read_bytes()
        stat = path.stat()
        identity = FileIdentity(
            relative_path=source.relative_path,
            device=stat.st_dev,
            inode=stat.st_ino,
            mode=stat.st_mode,
            uid=stat.st_uid,
            gid=stat.st_gid,
            size=stat.st_size,
            mtime_ns=stat.st_mtime_ns,
            sha256=hashlib.sha256(raw).hexdigest(),
        )
        identities[source.relative_path] = identity
        source_bytes[source.relative_path] = raw
    indexed_by_address = {item.address: item for item in snapshot.module.resources}
    observations: list[ResourceObservation] = []
    for observation in snapshot.resources:
        committed = observation.committed
        indexed = indexed_by_address.get(observation.address.absolute)
        if committed is not None and indexed is not None:
            identity = identities[indexed.source_path]
            raw = source_bytes[indexed.source_path]
            if indexed.block is None:
                block_bytes = b""
            else:
                block_bytes = raw[indexed.block.whole.start : indexed.block.whole.end]
            committed = SourceResource(
                observation.address,
                identity,
                bytes(block_bytes),
                committed.attributes,
                tuple(
                    SourceAttribute(
                        attribute.attribute_path,
                        bytes(raw[attribute.whole.start : attribute.whole.end]),
                        bytes(raw[attribute.expression.start : attribute.expression.end]),
                        attribute.literal,
                    )
                    for attribute in indexed.attributes
                ),
            )
        observations.append(replace(observation, committed=committed))
    return ReconcileSnapshot(
        resources=tuple(observations),
        module=snapshot.module,
        source_identities=tuple(sorted(identities.values(), key=lambda item: item.relative_path)),
        controller_digest=snapshot.controller_digest,
    )


def _thaw_object(value: FrozenObject) -> dict[str, object]:
    return {key: _thaw_value(item) for key, item in value.items}


def _thaw_value(value: FrozenValue) -> object:
    if isinstance(value, FrozenObject):
        return _thaw_object(value)
    if isinstance(value, tuple):
        return [_thaw_value(item) for item in value]
    return value
