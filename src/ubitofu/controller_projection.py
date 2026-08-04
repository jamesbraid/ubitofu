# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 James Braid
"""Project controller records onto explicit provider-owned comparison paths."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping

from .cleaner import clean_resource, normalize_emitted, strip_secret_shaped
from .enumerator import derive_identity
from .manifest import MANIFEST, ResourceSpec, spec_for_type
from .reconcile_model import (
    ControllerProjection,
    ControllerRecord,
    ControllerSnapshot,
    OpenTofuAddress,
    PlanDocument,
    ProjectedControllerResource,
    ProviderSchema,
    ReasonCode,
)
from .values import FrozenObject, FrozenValue, freeze_value


def project_controller_snapshot(
    *, plan: PlanDocument, controller: ControllerSnapshot, schema: ProviderSchema
) -> ControllerProjection:
    """Match raw records by manifest identity and expose only comparable provider paths."""
    schema_by_type = dict(schema.resources)
    candidates = _candidate_values(plan)
    matches: dict[tuple[str, str], list[tuple[OpenTofuAddress, FrozenObject]]] = {}
    reasons: set[ReasonCode] = set()
    covered = set(controller.covered_resource_types)

    for address, values in candidates.values():
        if address.resource_type not in {spec.resource_type for spec in MANIFEST}:
            reasons.add(ReasonCode.INCOMPARABLE_CONTROLLER_OBSERVATION)
            continue
        if address.resource_type not in covered:
            reasons.add(ReasonCode.STALE_CONTROLLER_OBSERVATION)
            continue
        try:
            spec = spec_for_type(address.resource_type)
            identity = _provider_identity(spec, values)
        except (KeyError, ValueError):
            reasons.add(ReasonCode.INCOMPARABLE_CONTROLLER_OBSERVATION)
            continue
        if identity is None:
            reasons.add(ReasonCode.INCOMPARABLE_CONTROLLER_OBSERVATION)
            continue
        matches.setdefault((address.resource_type, identity), []).append((address, values))

    projected: list[ProjectedControllerResource] = []
    seen_records: set[tuple[str, str]] = set()
    for record in sorted(controller.records, key=lambda item: (item.resource_type, item.import_id)):
        if record.resource_type not in covered:
            continue
        key = (record.resource_type, record.import_id)
        if key in seen_records:
            reasons.add(ReasonCode.INCOMPARABLE_CONTROLLER_OBSERVATION)
            continue
        seen_records.add(key)
        choices = matches.get(key, [])
        if len(choices) != 1:
            reasons.add(ReasonCode.INCOMPARABLE_CONTROLLER_OBSERVATION)
            continue
        address, managed = choices[0]
        resource_schema = schema_by_type.get(record.resource_type)
        if resource_schema is None:
            reasons.add(ReasonCode.INCOMPARABLE_CONTROLLER_OBSERVATION)
            continue
        try:
            spec = spec_for_type(record.resource_type)
            values, paths = _project_record(spec, record.raw, resource_schema)
        except (KeyError, TypeError, ValueError):
            reasons.add(ReasonCode.INCOMPARABLE_CONTROLLER_OBSERVATION)
            continue
        if not _managed_paths_covered(managed, resource_schema, paths):
            reasons.add(ReasonCode.INCOMPARABLE_CONTROLLER_OBSERVATION)
        projected.append(ProjectedControllerResource(address, values, paths))

    projected.sort(key=lambda item: item.address)
    blocking = tuple(sorted(reasons, key=lambda item: item.value))
    digest = _projection_digest(projected, blocking)
    return ControllerProjection(tuple(projected), blocking, digest)


def build_controller_snapshot(
    *,
    records: tuple[tuple[str, str, Mapping[str, object]], ...],
    covered_resource_types: tuple[str, ...],
) -> ControllerSnapshot:
    """Copy raw controller payloads into a deterministic immutable snapshot."""
    frozen_records: list[ControllerRecord] = []
    for resource_type, import_id, raw in records:
        frozen = freeze_value(raw)
        if not isinstance(frozen, FrozenObject):
            raise ValueError("controller record must be an object")
        frozen_records.append(ControllerRecord(resource_type, import_id, frozen))
    ordered = tuple(sorted(frozen_records, key=lambda item: (item.resource_type, item.import_id)))
    covered = tuple(sorted(set(covered_resource_types)))
    digest = hashlib.sha256(
        json.dumps(
            {
                "records": [
                    [item.resource_type, item.import_id, _thaw(item.raw)] for item in ordered
                ],
                "covered": covered,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    return ControllerSnapshot(ordered, covered, digest)


def _candidate_values(
    plan: PlanDocument,
) -> dict[str, tuple[OpenTofuAddress, FrozenObject]]:
    candidates = {
        address.absolute: (address, values) for address, values in plan.prior_state.resources
    }
    for change in plan.changes:
        value = change.after or change.before
        if value is not None:
            candidates[change.address.absolute] = (change.address, value)
    return candidates


def _provider_identity(spec: ResourceSpec, values: FrozenObject) -> str | None:
    raw = _object_dict(values)
    site = str(raw.get("site") or raw.get("id") or "")
    return derive_identity(spec.id_rule, raw, site)


def _project_record(
    spec: ResourceSpec, raw: FrozenObject, resource_schema: FrozenObject
) -> tuple[FrozenObject, tuple[tuple[str | int, ...], ...]]:
    raw_object = _object_dict(raw)
    schema_object = _object_dict(resource_schema)
    for policy in spec.controller_fields:
        value = _path_get(raw_object, policy.controller_path)
        if value is _MISSING:
            continue
        _path_set(raw_object, policy.provider_path, _coerce(value, policy.coercion))
    cleaned = clean_resource(raw_object, schema_object)
    normalize_emitted(spec.resource_type, cleaned)
    strip_secret_shaped(cleaned)
    excluded = _excluded_paths(schema_object)
    for path in excluded:
        _path_delete(cleaned, path)
    frozen = freeze_value(cleaned)
    if not isinstance(frozen, FrozenObject):
        raise ValueError("projected controller record must be an object")
    paths = tuple(sorted(_leaf_paths(cleaned)))
    return frozen, paths


def _managed_paths_covered(
    managed: FrozenObject,
    resource_schema: FrozenObject,
    comparable_paths: tuple[tuple[str | int, ...], ...],
) -> bool:
    managed_object = _object_dict(managed)
    schema_object = _object_dict(resource_schema)
    settable = _settable_top_level(schema_object)
    excluded = _excluded_paths(schema_object)
    required = {
        path
        for path in _leaf_paths(managed_object)
        if path
        and path[0] in settable
        and not any(path[: len(prefix)] == prefix for prefix in excluded)
    }
    return required.issubset(set(comparable_paths))


def _settable_top_level(schema: dict[str, object]) -> set[str]:
    block = schema.get("block")
    if not isinstance(block, dict):
        raise ValueError("provider schema block is missing")
    result: set[str] = set()
    attributes = block.get("attributes", {})
    if not isinstance(attributes, dict):
        raise ValueError("provider schema attributes are invalid")
    for name, item in attributes.items():
        if isinstance(item, dict) and (item.get("required") or item.get("optional")):
            result.add(name)
    block_types = block.get("block_types", {})
    if not isinstance(block_types, dict):
        raise ValueError("provider schema block types are invalid")
    result.update(block_types)
    return result


def _excluded_paths(schema: dict[str, object]) -> tuple[tuple[str | int, ...], ...]:
    block = schema.get("block")
    if not isinstance(block, dict):
        raise ValueError("provider schema block is missing")
    return tuple(_excluded_block_paths(block, ()))


def _excluded_block_paths(
    block: dict[str, object], prefix: tuple[str | int, ...]
) -> list[tuple[str | int, ...]]:
    excluded: list[tuple[str | int, ...]] = []
    attributes = block.get("attributes", {})
    if not isinstance(attributes, dict):
        raise ValueError("provider schema attributes are invalid")
    for name, item in attributes.items():
        if not isinstance(item, dict):
            raise ValueError("provider schema attribute is invalid")
        if item.get("sensitive") or item.get("write_only") or (
            item.get("computed") and not item.get("optional") and not item.get("required")
        ):
            excluded.append((*prefix, name))
            continue
        nested = item.get("nested_type")
        if isinstance(nested, dict):
            nested_attributes = nested.get("attributes")
            if isinstance(nested_attributes, dict):
                excluded.extend(
                    _excluded_block_paths(
                        {"attributes": nested_attributes}, (*prefix, name)
                    )
                )
    block_types = block.get("block_types", {})
    if not isinstance(block_types, dict):
        raise ValueError("provider schema block types are invalid")
    for name, item in block_types.items():
        if not isinstance(item, dict) or not isinstance(item.get("block"), dict):
            raise ValueError("provider schema block type is invalid")
        excluded.extend(_excluded_block_paths(item["block"], (*prefix, name)))
    return excluded


_MISSING = object()


def _path_get(value: object, path: tuple[str | int, ...]) -> object:
    current = value
    for part in path:
        if isinstance(part, str) and isinstance(current, dict) and part in current:
            current = current[part]
        elif isinstance(part, int) and isinstance(current, list) and 0 <= part < len(current):
            current = current[part]
        else:
            return _MISSING
    return current


def _path_set(value: dict[str, object], path: tuple[str | int, ...], replacement: object) -> None:
    if not path or not all(isinstance(part, str) for part in path):
        raise ValueError("controller provider path must address object fields")
    string_path = tuple(part for part in path if isinstance(part, str))
    current = value
    for part in string_path[:-1]:
        child = current.setdefault(part, {})
        if not isinstance(child, dict):
            raise ValueError("controller provider path conflicts with scalar")
        current = child
    current[string_path[-1]] = replacement


def _path_delete(value: dict[str, object], path: tuple[str | int, ...]) -> None:
    if len(path) == 1 and isinstance(path[0], str):
        value.pop(path[0], None)


def _coerce(value: object, coercion: str) -> object:
    if coercion == "identity":
        return value
    if coercion == "bool":
        if isinstance(value, bool):
            return value
        if isinstance(value, str) and value.lower() in {"true", "false"}:
            return value.lower() == "true"
        if isinstance(value, int) and value in {0, 1}:
            return bool(value)
        raise ValueError("controller boolean coercion failed")
    if coercion == "int":
        if isinstance(value, bool):
            raise ValueError("controller integer coercion failed")
        return int(value) if isinstance(value, str | int) else _raise_coercion("integer")
    if coercion == "string":
        if isinstance(value, str | int | float | bool):
            return str(value)
        raise ValueError("controller string coercion failed")
    if coercion == "set":
        if not isinstance(value, list | tuple):
            raise ValueError("controller set coercion failed")
        return sorted(set(value), key=repr)
    raise ValueError("unknown controller coercion")


def _raise_coercion(kind: str) -> object:
    raise ValueError(f"controller {kind} coercion failed")


def _leaf_paths(value: object, prefix: tuple[str | int, ...] = ()) -> set[tuple[str | int, ...]]:
    if isinstance(value, dict):
        return {
            path
            for key, item in value.items()
            for path in _leaf_paths(item, (*prefix, key))
        }
    if isinstance(value, list):
        return {
            path
            for index, item in enumerate(value)
            for path in _leaf_paths(item, (*prefix, index))
        }
    return {prefix}


def _object_dict(value: FrozenObject) -> dict[str, object]:
    thawed = _thaw(value)
    if not isinstance(thawed, dict):
        raise TypeError("frozen object did not thaw to an object")
    return thawed


def _thaw(value: FrozenValue) -> object:
    if isinstance(value, FrozenObject):
        return {key: _thaw(item) for key, item in value.items}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return value


def _projection_digest(
    projected: list[ProjectedControllerResource], blocking: tuple[ReasonCode, ...]
) -> str:
    payload = {
        "resources": [
            {
                "address": item.address.absolute,
                "values": _thaw(item.values),
                "paths": [list(path) for path in item.comparable_paths],
            }
            for item in projected
        ],
        "blocking": [reason.value for reason in blocking],
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
