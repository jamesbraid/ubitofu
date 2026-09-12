# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 James Braid
"""Project controller records onto explicit provider-owned comparison paths."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping

from .cleaner import apply_generation_normalization, is_settable
from .enumerator import ImportTarget, derive_identity
from .import_emitter import assign_slugs
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
    parse_opentofu_address,
)
from .secrets import SECRETS
from .values import FrozenObject, FrozenValue, freeze_value


def project_controller_snapshot(
    *, plan: PlanDocument, controller: ControllerSnapshot, schema: ProviderSchema
) -> ControllerProjection:
    """Match records and keep projection-wide blockers separate from local ones.

    ``ControllerProjection.blocking_reasons`` contains failures that cannot be
    attributed to one projected address. Addressable failures belong only on
    ``ProjectedControllerResource.blocking_reasons`` so an unrelated resource
    does not lose its own classification.
    """
    schema_by_type = dict(schema.resources)
    candidates = _candidate_values(plan)
    plan_time_live = dict(plan.plan_time_live)
    matches: dict[tuple[str, str], list[tuple[OpenTofuAddress, FrozenObject]]] = {}
    projection_blockers: set[ReasonCode] = set()
    projected_by_address: dict[OpenTofuAddress, ProjectedControllerResource] = {}
    covered = set(controller.covered_resource_types)

    manifest_types = {spec.resource_type for spec in MANIFEST}
    candidate_identities: dict[OpenTofuAddress, tuple[str, str]] = {}
    for address, values in candidates.items():
        if address.deposed is not None:
            continue
        if address.resource_type not in manifest_types:
            _add_projected(
                projected_by_address,
                _blocked_projection(
                    address, ReasonCode.INCOMPARABLE_CONTROLLER_OBSERVATION
                ),
            )
            continue
        if address.resource_type not in covered:
            _add_projected(
                projected_by_address,
                _blocked_projection(address, ReasonCode.STALE_CONTROLLER_OBSERVATION),
            )
            continue
        try:
            spec = spec_for_type(address.resource_type)
            identity = _provider_identity(spec, values)
        except (KeyError, ValueError):
            _add_projected(
                projected_by_address,
                _blocked_projection(
                    address, ReasonCode.INCOMPARABLE_CONTROLLER_OBSERVATION
                ),
            )
            continue
        if identity is None:
            _add_projected(
                projected_by_address,
                _blocked_projection(
                    address, ReasonCode.INCOMPARABLE_CONTROLLER_OBSERVATION
                ),
            )
            continue
        key = (address.resource_type, identity)
        candidate_identities[address] = key
        matches.setdefault(key, []).append((address, values))

    ambiguous_keys = {key for key, choices in matches.items() if len(choices) > 1}
    for key in ambiguous_keys:
        _block_choices(projected_by_address, matches[key], key[1])

    seen_records: set[tuple[str, str]] = set()
    unmatched_records: list[
        tuple[
            ControllerRecord,
            FrozenObject,
            tuple[tuple[str | int, ...], ...],
            tuple[ReasonCode, ...],
        ]
    ] = []
    matched_keys: set[tuple[str, str]] = set()
    for record in sorted(
        controller.records,
        key=lambda item: (item.resource_type, item.import_id, item.name_hint or ""),
    ):
        if record.resource_type not in covered:
            continue
        key = (record.resource_type, record.import_id)
        choices = matches.get(key, [])
        if key in seen_records:
            if choices:
                _block_choices(projected_by_address, choices, record.import_id)
            else:
                projection_blockers.add(
                    ReasonCode.INCOMPARABLE_CONTROLLER_OBSERVATION
                )
            continue
        seen_records.add(key)
        if len(choices) > 1:
            _block_choices(projected_by_address, choices, record.import_id)
            continue
        resource_schema = schema_by_type.get(record.resource_type)
        if resource_schema is None:
            if choices:
                _block_choices(projected_by_address, choices, record.import_id)
            else:
                projection_blockers.add(
                    ReasonCode.INCOMPARABLE_CONTROLLER_OBSERVATION
                )
            continue
        try:
            spec = spec_for_type(record.resource_type)
            values, paths = _project_record(spec, record.raw, resource_schema)
        except (KeyError, TypeError, ValueError):
            if choices:
                _block_choices(projected_by_address, choices, record.import_id)
            else:
                projection_blockers.add(
                    ReasonCode.INCOMPARABLE_CONTROLLER_OBSERVATION
                )
            continue
        if not choices:
            unmatched_blockers = _controller_only_blockers(
                spec, values, resource_schema
            )
            unmatched_records.append((record, values, paths, unmatched_blockers))
            continue
        address, managed = choices[0]
        matched_keys.add(key)
        blockers: set[ReasonCode] = set()
        if not _managed_paths_covered(spec, managed, resource_schema, paths):
            blockers.add(ReasonCode.INCOMPARABLE_CONTROLLER_OBSERVATION)
        planned = plan_time_live.get(address)
        if planned is None:
            blockers.add(ReasonCode.STALE_CONTROLLER_OBSERVATION)
        else:
            planned_values, planned_paths = _project_provider_value(
                spec, planned, resource_schema
            )
            if planned_paths != paths:
                blockers.add(ReasonCode.INCOMPARABLE_CONTROLLER_OBSERVATION)
            elif planned_values != values:
                blockers.add(ReasonCode.STALE_CONTROLLER_OBSERVATION)
        _add_projected(
            projected_by_address,
            ProjectedControllerResource(
                address,
                values,
                paths,
                True,
                record.import_id,
                tuple(sorted(blockers, key=lambda item: item.value)),
            ),
        )

    reserved = {address.absolute for address in candidates}
    targets: list[ImportTarget] = []
    unmatched_by_key: dict[
        tuple[str, str],
        tuple[
            FrozenObject,
            tuple[tuple[str | int, ...], ...],
            tuple[ReasonCode, ...],
        ],
    ] = {}
    for record, values, paths, unmatched_reasons in unmatched_records:
        if record.name_hint is None:
            projection_blockers.add(ReasonCode.INCOMPARABLE_CONTROLLER_OBSERVATION)
            continue
        target = ImportTarget(record.resource_type, record.name_hint, record.import_id)
        targets.append(target)
        unmatched_by_key[(record.resource_type, record.import_id)] = (
            values,
            paths,
            unmatched_reasons,
        )
    for target, slug in assign_slugs(targets, reserved=reserved):
        values, paths, unmatched_reasons = unmatched_by_key[
            (target.resource_type, target.import_id)
        ]
        address = parse_opentofu_address(f"{target.resource_type}.{slug}")
        _add_projected(
            projected_by_address,
            ProjectedControllerResource(
                address, values, paths, True, target.import_id, unmatched_reasons
            ),
        )

    for address, key in candidate_identities.items():
        if key in ambiguous_keys or key in matched_keys or key in seen_records:
            continue
        planned = plan_time_live.get(address)
        absence_blockers = (
            (ReasonCode.STALE_CONTROLLER_OBSERVATION,) if planned is not None else ()
        )
        _add_projected(
            projected_by_address,
            ProjectedControllerResource(
                address, None, (), False, key[1], absence_blockers
            ),
        )

    projected = sorted(projected_by_address.values(), key=lambda item: item.address)
    blocking = tuple(sorted(projection_blockers, key=lambda item: item.value))
    digest = _projection_digest(projected, blocking)
    return ControllerProjection(tuple(projected), blocking, digest)


def _blocked_projection(
    address: OpenTofuAddress,
    reason: ReasonCode,
    *,
    present: bool = False,
    import_id: str | None = None,
) -> ProjectedControllerResource:
    return ProjectedControllerResource(
        address,
        None,
        (),
        present,
        import_id,
        (reason,),
    )


def _block_choices(
    projected: dict[OpenTofuAddress, ProjectedControllerResource],
    choices: list[tuple[OpenTofuAddress, FrozenObject]],
    import_id: str,
) -> None:
    for address, _ in choices:
        _add_projected(
            projected,
            _blocked_projection(
                address,
                ReasonCode.INCOMPARABLE_CONTROLLER_OBSERVATION,
                present=True,
                import_id=import_id,
            ),
        )


def _add_projected(
    projected: dict[OpenTofuAddress, ProjectedControllerResource],
    incoming: ProjectedControllerResource,
) -> None:
    existing = projected.get(incoming.address)
    if existing is None:
        projected[incoming.address] = incoming
        return
    use_incoming_values = existing.values is None and incoming.values is not None
    projected[incoming.address] = ProjectedControllerResource(
        incoming.address,
        incoming.values if use_incoming_values else existing.values,
        incoming.comparable_paths if use_incoming_values else existing.comparable_paths,
        existing.present or incoming.present,
        existing.import_id or incoming.import_id,
        tuple(
            sorted(
                {*existing.blocking_reasons, *incoming.blocking_reasons},
                key=lambda item: item.value,
            )
        ),
    )


def build_controller_snapshot(
    *,
    records: tuple[tuple[str, str, Mapping[str, object]], ...],
    covered_resource_types: tuple[str, ...],
    name_hints: Mapping[tuple[str, str], str] | None = None,
) -> ControllerSnapshot:
    """Copy raw controller payloads into a deterministic immutable snapshot."""
    frozen_records: list[ControllerRecord] = []
    for resource_type, import_id, raw in records:
        frozen = freeze_value(raw)
        if not isinstance(frozen, FrozenObject):
            raise ValueError("controller record must be an object")
        hint = None if name_hints is None else name_hints.get((resource_type, import_id))
        frozen_records.append(ControllerRecord(resource_type, import_id, frozen, hint))
    ordered = tuple(
        sorted(
            frozen_records,
            key=lambda item: (item.resource_type, item.import_id, item.name_hint or ""),
        )
    )
    covered = tuple(sorted(set(covered_resource_types)))
    digest = hashlib.sha256(
        json.dumps(
            {
                "records": [
                    [item.resource_type, item.import_id, item.name_hint, _thaw(item.raw)]
                    for item in ordered
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
) -> dict[OpenTofuAddress, FrozenObject]:
    candidates = {
        address: values for address, values in plan.prior_state.resources
    }
    for change in plan.changes:
        value = change.after or change.before
        if value is not None:
            candidates[change.address] = value
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
    return _project_provider_mapping(spec, raw_object, schema_object)


def _project_provider_value(
    spec: ResourceSpec, value: FrozenObject, resource_schema: FrozenObject
) -> tuple[FrozenObject, tuple[tuple[str | int, ...], ...]]:
    return _project_provider_mapping(
        spec, _object_dict(value), _object_dict(resource_schema)
    )


def _project_provider_mapping(
    spec: ResourceSpec, value: dict[str, object], schema: dict[str, object]
) -> tuple[FrozenObject, tuple[tuple[str | int, ...], ...]]:
    block = schema.get("block")
    if not isinstance(block, dict):
        raise ValueError("provider schema block is missing")
    cleaned = _project_block(value, block)
    apply_generation_normalization(spec.generation_normalization, cleaned)
    frozen = freeze_value(cleaned)
    if not isinstance(frozen, FrozenObject):
        raise ValueError("projected controller record must be an object")
    paths = tuple(sorted(_leaf_paths(cleaned)))
    return frozen, paths


def _managed_paths_covered(
    spec: ResourceSpec,
    managed: FrozenObject,
    resource_schema: FrozenObject,
    comparable_paths: tuple[tuple[str | int, ...], ...],
) -> bool:
    _, required_paths = _project_provider_value(spec, managed, resource_schema)
    required = set(required_paths)
    return required.issubset(set(comparable_paths))


def _controller_only_blockers(
    spec: ResourceSpec,
    projected: FrozenObject,
    resource_schema: FrozenObject,
) -> tuple[ReasonCode, ...]:
    values = _object_dict(projected)
    schema = _object_dict(resource_schema)
    block = schema.get("block")
    if not isinstance(block, dict):
        raise ValueError("provider schema block is missing")
    if _missing_required_paths(spec.resource_type, values, block, ()):
        return (ReasonCode.INCOMPARABLE_CONTROLLER_OBSERVATION,)
    return ()


def _missing_required_paths(
    resource_type: str,
    values: dict[str, object],
    block: dict[str, object],
    prefix: tuple[str | int, ...],
) -> bool:
    attributes = block.get("attributes", {})
    if not isinstance(attributes, dict):
        raise ValueError("provider schema attributes are invalid")
    for name, item in attributes.items():
        if not isinstance(name, str) or not isinstance(item, dict):
            raise ValueError("provider schema attribute is invalid")
        path = (*prefix, name)
        if not is_settable(item):
            continue
        excluded = bool(item.get("sensitive") or item.get("write_only"))
        if item.get("required"):
            if excluded:
                if not _has_secret_binding(resource_type, path):
                    return True
                continue
            if name not in values or values[name] is None:
                return True
        nested = item.get("nested_type")
        if not isinstance(nested, dict) or name not in values:
            continue
        nested_attributes = nested.get("attributes")
        if not isinstance(nested_attributes, dict):
            raise ValueError("provider nested attributes are invalid")
        nested_block: dict[str, object] = {"attributes": nested_attributes}
        raw = values[name]
        if nested.get("nesting_mode") == "single":
            if not isinstance(raw, dict) or _missing_required_paths(
                resource_type, raw, nested_block, path
            ):
                return True
            continue
        if not isinstance(raw, list):
            return True
        for index, entry in enumerate(raw):
            if not isinstance(entry, dict) or _missing_required_paths(
                resource_type, entry, nested_block, (*path, index)
            ):
                return True
    block_types = block.get("block_types", {})
    if not isinstance(block_types, dict):
        raise ValueError("provider schema block types are invalid")
    for name, item in block_types.items():
        if not isinstance(name, str) or not isinstance(item, dict):
            raise ValueError("provider schema block type is invalid")
        block_type_body = item.get("block")
        min_items = item.get("min_items", 0)
        if (
            not isinstance(block_type_body, dict)
            or not isinstance(min_items, int)
            or isinstance(min_items, bool)
            or min_items < 0
        ):
            raise ValueError("provider schema block type is invalid")
        if name not in values:
            if min_items > 0:
                return True
            continue
        raw = values[name]
        if not isinstance(raw, list) or len(raw) < min_items:
            return True
        for index, entry in enumerate(raw):
            if not isinstance(entry, dict) or _missing_required_paths(
                resource_type, entry, block_type_body, (*prefix, name, index)
            ):
                return True
    return False


def _has_secret_binding(
    resource_type: str, path: tuple[str | int, ...]
) -> bool:
    attribute = ".".join(str(part) for part in path if not isinstance(part, int))
    return any(
        rule.resource_type == resource_type and rule.attr == attribute
        for rule in SECRETS
    )


def _project_block(value: dict[str, object], block: dict[str, object]) -> dict[str, object]:
    projected: dict[str, object] = {}
    attributes = block.get("attributes", {})
    if not isinstance(attributes, dict):
        raise ValueError("provider schema attributes are invalid")
    for name, item in attributes.items():
        if not isinstance(item, dict):
            raise ValueError("provider schema attribute is invalid")
        if item.get("sensitive") or item.get("write_only"):
            continue
        if not is_settable(item) or name not in value:
            continue
        raw = value[name]
        nested = item.get("nested_type")
        if isinstance(nested, dict):
            if raw is None:
                # State holds `null` for an unset nested object (every
                # resource's `timeouts`, for one). Nothing to compare.
                continue
            nested_attributes = nested.get("attributes")
            if not isinstance(nested_attributes, dict):
                raise ValueError("provider nested attributes are invalid")
            nested_block: dict[str, object] = {"attributes": nested_attributes}
            mode = nested.get("nesting_mode")
            if mode == "single":
                if not isinstance(raw, dict):
                    raise ValueError("provider nested object is invalid")
                projected[name] = _project_block(raw, nested_block)
            else:
                if not isinstance(raw, list):
                    raise ValueError("provider nested collection is invalid")
                projected[name] = [
                    _project_block(item_value, nested_block)
                    for item_value in raw
                    if isinstance(item_value, dict)
                ]
            continue
        if not _secret_shaped(name, raw):
            projected[name] = raw
    block_types = block.get("block_types", {})
    if not isinstance(block_types, dict):
        raise ValueError("provider schema block types are invalid")
    for name, item in block_types.items():
        if not isinstance(item, dict) or not isinstance(item.get("block"), dict):
            raise ValueError("provider schema block type is invalid")
        if name not in value:
            continue
        raw = value[name]
        if not isinstance(raw, list):
            raw = [raw]
        projected[name] = [
            _project_block(entry, item["block"])
            for entry in raw
            if isinstance(entry, dict)
        ]
    return projected


_SECRET_NAME = re.compile(
    r"credential|private_key|passphrase|secret|token|password|api_key", re.IGNORECASE
)
_PUBLIC_NAME = re.compile(r"public", re.IGNORECASE)
_B64_KEY = re.compile(r"^[A-Za-z0-9+/]{43}=$")


def _secret_shaped(name: str, value: object) -> bool:
    if isinstance(value, str):
        if not value or _PUBLIC_NAME.search(name):
            return False
        return bool(_SECRET_NAME.search(name) or _B64_KEY.fullmatch(value))
    if isinstance(value, dict):
        return any(_secret_shaped(str(key), item) for key, item in value.items())
    if isinstance(value, list):
        return any(_secret_shaped(name, item) for item in value)
    return False


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
        if not value:
            return {prefix}
        return {
            path
            for key, item in value.items()
            for path in _leaf_paths(item, (*prefix, key))
        }
    if isinstance(value, list):
        if not value:
            return {prefix}
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
                "address": _address_payload(item.address),
                "values": _thaw(item.values),
                "paths": [list(path) for path in item.comparable_paths],
                "present": item.present,
                "import_id": item.import_id,
                "blocking": [reason.value for reason in item.blocking_reasons],
            }
            for item in projected
        ],
        "blocking": [reason.value for reason in blocking],
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _address_payload(address: OpenTofuAddress) -> dict[str, object]:
    return {
        "absolute": address.absolute,
        "module": address.module,
        "mode": address.mode,
        "resource_type": address.resource_type,
        "name": address.name,
        "index": address.index,
        "deposed": address.deposed,
    }
