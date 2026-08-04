# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 James Braid
"""Strict headers for JSON emitted by the OpenTofu subprocess boundary."""

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

from .errors import ExternalDocumentError
from .reconcile_model import (
    ActionVector,
    OpenTofuAddress,
    PlanDocument,
    ProviderSchema,
    ResourceChange,
    StateDocument,
    parse_opentofu_address,
)
from .values import FrozenObject, freeze_value

_FORMAT_VERSION = re.compile(r"^(\d+)\.(\d+)$")


@dataclass(frozen=True)
class DocumentHeader:
    kind: Literal["plan", "state", "provider_schema"]
    format_version: tuple[int, int]


def validate_document_header(
    value: object,
    *,
    kind: Literal["plan", "state", "provider_schema"],
) -> tuple[DocumentHeader, Mapping[str, object]]:
    if not isinstance(value, Mapping):
        raise ExternalDocumentError(kind, "document", "invalid document")
    version = value.get("format_version")
    if not isinstance(version, str):
        raise ExternalDocumentError(kind, "format_version", "missing field")
    match = _FORMAT_VERSION.fullmatch(version)
    if match is None:
        raise ExternalDocumentError(kind, "format_version", "invalid document")
    parsed = (int(match.group(1)), int(match.group(2)))
    if parsed[0] != 1:
        raise ExternalDocumentError(kind, "format_version", "unsupported format version")
    if kind == "plan":
        errored = value.get("errored")
        if not isinstance(errored, bool):
            raise ExternalDocumentError(kind, "errored", "missing field")
        if errored:
            raise ExternalDocumentError(kind, "errored", "plan reported errors")
    return DocumentHeader(kind, parsed), value


def parse_plan_document(value: object) -> PlanDocument:
    """Decode only reconciliation facts from a saved OpenTofu plan document."""
    header, document = validate_document_header(value, kind="plan")
    prior_raw = _mapping_field(document, "prior_state", kind="plan")
    prior_state = parse_state_document(prior_raw)
    primary = _change_list(document, "resource_changes", required=True)
    drift = _change_list(document, "resource_drift", required=False)
    changes_by_address = {change.address: change for change in drift}
    changes_by_address.update((change.address, change) for change in primary)
    live_by_address = {change.address: change.before for change in drift}
    live_by_address.update((change.address, change.before) for change in primary)
    return PlanDocument(
        format_version=header.format_version,
        prior_state=prior_state,
        changes=tuple(changes_by_address[key] for key in sorted(changes_by_address)),
        plan_time_live=tuple(sorted(live_by_address.items(), key=lambda item: item[0])),
    )


def parse_state_document(value: object) -> StateDocument:
    """Decode resource identities and provider values from a state-shaped document."""
    _, document = validate_document_header(value, kind="state")
    values = _mapping_field(document, "values", kind="state")
    root = values.get("root_module")
    if root is None:
        return StateDocument(())
    if not isinstance(root, Mapping):
        raise ExternalDocumentError("state", "root_module", "invalid document")
    resources = _state_module_resources(root)
    by_address: dict[
        OpenTofuAddress, tuple[OpenTofuAddress, FrozenObject, FrozenObject]
    ] = {}
    for address, frozen, sensitive in resources:
        if address in by_address:
            raise ExternalDocumentError("state", "address", "invalid document")
        by_address[address] = (address, frozen, sensitive)
    ordered = tuple(by_address[address] for address in sorted(by_address))
    return StateDocument(
        tuple((address, frozen) for address, frozen, _ in ordered),
        tuple((address, sensitive) for address, _, sensitive in ordered),
    )


def parse_provider_schema(value: object) -> ProviderSchema:
    """Decode resource schema objects while ignoring provider JSON minor extensions."""
    _, document = validate_document_header(value, kind="provider_schema")
    providers = _mapping_field(document, "provider_schemas", kind="provider_schema")
    resources: dict[str, FrozenObject] = {}
    for provider in providers.values():
        if not isinstance(provider, Mapping):
            raise ExternalDocumentError("provider_schema", "provider", "invalid document")
        raw_resources = provider.get("resource_schemas", {})
        if not isinstance(raw_resources, Mapping):
            raise ExternalDocumentError(
                "provider_schema", "resource_schemas", "invalid document"
            )
        for resource_type, raw_schema in raw_resources.items():
            if not isinstance(resource_type, str):
                raise ExternalDocumentError(
                    "provider_schema", "resource_type", "invalid document"
                )
            schema = _frozen_object(raw_schema, "provider_schema", "resource_schema")
            existing = resources.get(resource_type)
            if existing is not None and existing != schema:
                raise ExternalDocumentError(
                    "provider_schema", "resource_type", "invalid document"
                )
            resources[resource_type] = schema
    return ProviderSchema(tuple(sorted(resources.items())))


def _state_module_resources(
    module: Mapping[str, object],
) -> list[tuple[OpenTofuAddress, FrozenObject, FrozenObject]]:
    resources: list[tuple[OpenTofuAddress, FrozenObject, FrozenObject]] = []
    raw_resources = module.get("resources", [])
    if not isinstance(raw_resources, list):
        raise ExternalDocumentError("state", "resources", "invalid document")
    for row in raw_resources:
        row_map = _mapping(row, "state", "resource")
        address = _address(row_map, kind="state")
        resources.append((
            address,
            _frozen_object(row_map.get("values"), "state", "values"),
            _sensitivity_mask(
                row_map.get("sensitive_values", {}), "state", "sensitive_values"
            ),
        ))
    children = module.get("child_modules", [])
    if not isinstance(children, list):
        raise ExternalDocumentError("state", "child_modules", "invalid document")
    for child in children:
        resources.extend(_state_module_resources(_mapping(child, "state", "child_modules")))
    return resources


def _change_list(
    document: Mapping[str, object], field: str, *, required: bool
) -> tuple[ResourceChange, ...]:
    raw = document.get(field)
    if raw is None and not required:
        return ()
    if not isinstance(raw, list):
        reason = "missing field" if raw is None else "invalid document"
        raise ExternalDocumentError("plan", field, reason)
    return tuple(_resource_change(item) for item in raw)


def _resource_change(value: object) -> ResourceChange:
    row = _mapping(value, "plan", "resource_change")
    address = _address(row, kind="plan")
    change = _mapping_field(row, "change", kind="plan")
    actions = change.get("actions")
    if not isinstance(actions, list) or not all(isinstance(item, str) for item in actions):
        raise ExternalDocumentError("plan", "actions", "invalid document")
    try:
        action = ActionVector(tuple(actions))
    except ValueError as error:
        raise ExternalDocumentError("plan", "actions", "invalid document") from error
    before = _optional_frozen_object(change.get("before"), "before")
    after = _optional_frozen_object(change.get("after"), "after")
    after_unknown = _frozen_object(change.get("after_unknown"), "plan", "after_unknown")
    before_sensitive = _optional_mask(change, "before_sensitive")
    after_sensitive = _optional_mask(change, "after_sensitive")
    return ResourceChange(
        address,
        action,
        before,
        after,
        after_unknown,
        before_sensitive,
        after_sensitive,
    )


def _optional_mask(value: Mapping[str, object], field: str) -> FrozenObject:
    raw = value.get(field, {})
    return _sensitivity_mask(raw, "plan", field)


def _sensitivity_mask(value: object, kind: str, field: str) -> FrozenObject:
    if not isinstance(value, Mapping) or not all(isinstance(key, str) for key in value):
        raise ExternalDocumentError(kind, field, "invalid document")
    _validate_sensitivity_mask(value, kind, field)
    return _frozen_object(value, kind, field)


def _validate_sensitivity_mask(value: object, kind: str, field: str) -> None:
    if isinstance(value, Mapping):
        if not all(isinstance(key, str) for key in value):
            raise ExternalDocumentError(kind, field, "invalid document")
        for item in value.values():
            _validate_sensitivity_mask(item, kind, field)
        return
    if isinstance(value, list):
        for item in value:
            _validate_sensitivity_mask(item, kind, field)
        return
    if value is not True:
        raise ExternalDocumentError(kind, field, "invalid document")


def _address(
    row: Mapping[str, object], *, kind: Literal["plan", "state"]
) -> OpenTofuAddress:
    absolute = row.get("address")
    mode = row.get("mode")
    resource_type = row.get("type")
    name = row.get("name")
    module = row.get("module_address")
    index = row.get("index")
    deposed = row.get("deposed")
    if (
        not isinstance(absolute, str)
        or mode not in {"managed", "data"}
        or not isinstance(resource_type, str)
        or not isinstance(name, str)
        or (module is not None and not isinstance(module, str))
        or (index is not None and not isinstance(index, str | int))
        or isinstance(index, bool)
        or (deposed is not None and not isinstance(deposed, str))
    ):
        raise ExternalDocumentError(kind, "address", "invalid document")
    try:
        return parse_opentofu_address(
            absolute,
            module=module,
            mode=mode,
            resource_type=resource_type,
            name=name,
            index=index,
            deposed=deposed,
        )
    except ValueError as error:
        raise ExternalDocumentError(kind, "address", "invalid document") from error


def _mapping_field(
    value: Mapping[str, object], field: str, *, kind: Literal["plan", "state", "provider_schema"]
) -> Mapping[str, object]:
    return _mapping(value.get(field), kind, field)


def _mapping(value: object, kind: str, field: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or not all(isinstance(key, str) for key in value):
        raise ExternalDocumentError(kind, field, "invalid document")
    return value


def _frozen_object(value: object, kind: str, field: str) -> FrozenObject:
    try:
        frozen = freeze_value(value)
    except ValueError as error:
        raise ExternalDocumentError(kind, field, "invalid document") from error
    if not isinstance(frozen, FrozenObject):
        raise ExternalDocumentError(kind, field, "invalid document")
    return frozen


def _optional_frozen_object(value: object, field: str) -> FrozenObject | None:
    return None if value is None else _frozen_object(value, "plan", field)
