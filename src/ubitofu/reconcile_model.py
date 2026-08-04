# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 James Braid
"""Immutable observations and edit intents for three-way reconciliation."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from enum import Enum
from pathlib import PurePosixPath
from typing import Literal, TypeAlias

from .errors import UbitofuError
from .module_index import ModuleIndex
from .values import FrozenObject


class ActionVector(Enum):
    NOOP = ("no-op",)
    CREATE = ("create",)
    READ = ("read",)
    UPDATE = ("update",)
    DELETE = ("delete",)
    FORGET = ("forget",)
    DELETE_CREATE = ("delete", "create")
    CREATE_DELETE = ("create", "delete")


@dataclass(frozen=True)
class OpenTofuAddress:
    absolute: str
    module: str | None
    mode: Literal["managed", "data"]
    resource_type: str
    name: str
    index: str | int | None
    deposed: str | None

    def __lt__(self, other: object) -> bool:
        if not isinstance(other, OpenTofuAddress):
            return NotImplemented
        return _address_sort_key(self) < _address_sort_key(other)


def _address_sort_key(address: OpenTofuAddress) -> tuple[object, ...]:
    index = address.index
    index_key = (
        0 if index is None else 1 if isinstance(index, int) else 2,
        "" if index is None else index,
    )
    return (
        address.absolute,
        address.module or "",
        address.mode,
        address.resource_type,
        address.name,
        index_key,
        address.deposed is not None,
        address.deposed or "",
    )


_NAME = r"[A-Za-z_][A-Za-z0-9_-]*"
_INDEX = r'(?:0|[1-9][0-9]*|"(?:[^"\\]|\\.)*")'
_MODULE = rf"module\.{_NAME}(?:\[{_INDEX}\])?"
_ADDRESS = re.compile(
    rf"^(?P<modules>(?:{_MODULE}\.)*)(?P<data>data\.)?"
    rf"(?P<type>{_NAME})\.(?P<name>{_NAME})(?:\[(?P<index>{_INDEX})\])?$"
)


def parse_opentofu_address(
    absolute: str,
    *,
    module: str | None = None,
    mode: Literal["managed", "data"] | None = None,
    resource_type: str | None = None,
    name: str | None = None,
    index: str | int | None = None,
    deposed: str | None = None,
) -> OpenTofuAddress:
    """Parse supported identity fields while retaining the original address verbatim."""
    match = _ADDRESS.fullmatch(absolute)
    if match is None:
        if mode is None or resource_type is None or name is None:
            raise ValueError("unsupported OpenTofu address shape")
        return OpenTofuAddress(
            absolute, module, mode, resource_type, name, index, deposed
        )
    parsed_modules = match.group("modules").removesuffix(".") or None
    parsed_index: str | int | None = None
    raw_index = match.group("index")
    if raw_index is not None:
        parsed_index = json.loads(raw_index) if raw_index.startswith('"') else int(raw_index)
    return OpenTofuAddress(
        absolute=absolute,
        module=parsed_modules if module is None else module,
        mode=("data" if match.group("data") else "managed") if mode is None else mode,
        resource_type=match.group("type") if resource_type is None else resource_type,
        name=match.group("name") if name is None else name,
        index=parsed_index if index is None else index,
        deposed=deposed,
    )


@dataclass(frozen=True)
class ResourceChange:
    address: OpenTofuAddress
    action: ActionVector
    before: FrozenObject | None
    after: FrozenObject | None
    after_unknown: FrozenObject


@dataclass(frozen=True)
class StateDocument:
    resources: tuple[tuple[OpenTofuAddress, FrozenObject], ...]


@dataclass(frozen=True)
class PlanDocument:
    format_version: tuple[int, int]
    prior_state: StateDocument
    changes: tuple[ResourceChange, ...]
    plan_time_live: tuple[tuple[OpenTofuAddress, FrozenObject | None], ...]


@dataclass(frozen=True)
class ProviderSchema:
    resources: tuple[tuple[str, FrozenObject], ...]


@dataclass(frozen=True)
class ControllerRecord:
    resource_type: str
    import_id: str
    raw: FrozenObject
    name_hint: str | None = None


@dataclass(frozen=True)
class ControllerSnapshot:
    records: tuple[ControllerRecord, ...]
    covered_resource_types: tuple[str, ...]
    canonical_sha256: str


@dataclass(frozen=True)
class ProjectedControllerResource:
    address: OpenTofuAddress
    values: FrozenObject | None
    comparable_paths: tuple[tuple[str | int, ...], ...]
    present: bool = True
    import_id: str | None = None
    blocking_reasons: tuple[ReasonCode, ...] = ()


class ReasonCode(Enum):
    NO_CHANGE = "no_change"
    CODE_ONLY_CHANGE = "code_only_change"
    LIVE_ONLY_CHANGE = "live_only_change"
    CONCURRENT_CHANGE_CONVERGED = "concurrent_change_converged"
    CONCURRENT_VALUE_CONFLICT = "concurrent_value_conflict"
    COMPUTED_OR_UNKNOWN = "computed_or_unknown"
    SECRET_SUPPRESSED = "secret_suppressed"
    LIVE_RESOURCE_NEW = "live_resource_new"
    CONTROLLER_RESOURCE_DELETED = "controller_resource_deleted"
    PENDING_CREATE = "pending_create"
    PENDING_DELETE = "pending_delete"
    PENDING_FORGET = "pending_forget"
    STATE_ORPHANED = "state_orphaned"
    COMMITTED_NOT_IN_STATE = "committed_not_in_state"
    FORBIDDEN_DEVICE_CREATE = "forbidden_device_create"
    UNSUPPORTED_ADDRESS = "unsupported_address"
    STALE_CONTROLLER_OBSERVATION = "stale_controller_observation"
    INCOMPARABLE_CONTROLLER_OBSERVATION = "incomparable_controller_observation"
    UNSTABLE_COLLECTION_IDENTITY = "unstable_collection_identity"
    DELETE_MODIFY_CONFLICT = "delete_modify_conflict"
    REPLACEMENT_REQUIRES_ATTENTION = "replacement_requires_attention"
    JSON_SOURCE_READ_ONLY = "json_source_read_only"
    DANGLING_REFERENCE = "dangling_reference"
    DECLARED_COMPLEX_DRIFT = "declared_complex_drift"
    SOURCE_OWNERSHIP_AMBIGUOUS = "source_ownership_ambiguous"


@dataclass(frozen=True)
class ControllerProjection:
    resources: tuple[ProjectedControllerResource, ...]
    blocking_reasons: tuple[ReasonCode, ...]
    canonical_sha256: str


@dataclass(frozen=True)
class FileIdentity:
    relative_path: PurePosixPath
    device: int
    inode: int
    mode: int
    uid: int
    gid: int
    size: int
    mtime_ns: int
    sha256: str


@dataclass(frozen=True)
class SourceAttribute:
    attribute_path: tuple[str | int, ...]
    whole_bytes: bytes
    expression_bytes: bytes
    literal: bool


@dataclass(frozen=True)
class SourceResource:
    address: OpenTofuAddress
    file: FileIdentity
    block_bytes: bytes
    attributes: FrozenObject
    source_attributes: tuple[SourceAttribute, ...] = ()


@dataclass(frozen=True)
class LifecyclePolicy:
    create_in_ui_only: bool
    deletion_policy: Literal["capture", "attention", "forbid"]


@dataclass(frozen=True)
class GenerationNormalizationPolicy:
    rule: Literal["identity", "port_forward_wan_all_to_both"]


@dataclass(frozen=True)
class CollectionIdentityPolicy:
    attribute_path: tuple[str | int, ...]
    identity_attribute: str


@dataclass(frozen=True)
class ControllerFieldPolicy:
    controller_path: tuple[str | int, ...]
    provider_path: tuple[str | int, ...]
    coercion: Literal["identity", "bool", "int", "string", "set"]


@dataclass(frozen=True)
class ResourceObservation:
    address: OpenTofuAddress
    committed: SourceResource | None
    base: FrozenObject | None
    desired: FrozenObject | None
    live: FrozenObject | None
    change: ResourceChange | None
    lifecycle: LifecyclePolicy
    collection_identities: tuple[CollectionIdentityPolicy, ...]
    blocking_reasons: tuple[ReasonCode, ...] = ()
    fresh_present: bool | None = None
    fresh: FrozenObject | None = None
    import_id: str | None = None


@dataclass(frozen=True)
class ReconcileSnapshot:
    resources: tuple[ResourceObservation, ...]
    module: ModuleIndex
    source_identities: tuple[FileIdentity, ...]
    controller_digest: str


class Disposition(Enum):
    NO_CHANGE = "no_change"
    PRESERVE_CODE = "preserve_code"
    CAPTURE_LIVE = "capture_live"
    APPEND = "append"
    REMOVE = "remove"
    CONFLICT = "conflict"
    ATTENTION = "attention"
    FORBIDDEN = "forbidden"


@dataclass(frozen=True)
class SourceAnchor:
    address: OpenTofuAddress
    attribute_path: tuple[str | int, ...] | None
    expected_literal: bytes


@dataclass(frozen=True)
class UpdateScalar:
    address: OpenTofuAddress
    anchor: SourceAnchor
    replacement: bytes

    def __post_init__(self) -> None:
        if self.anchor.address != self.address or self.anchor.attribute_path is None:
            raise ValueError("replace intent requires a matching attribute anchor")


@dataclass(frozen=True)
class AddAttribute:
    address: OpenTofuAddress
    attribute_path: tuple[str | int, ...]
    anchor: SourceAnchor
    value: bytes

    def __post_init__(self) -> None:
        if (
            not self.attribute_path
            or self.anchor.address != self.address
            or self.anchor.attribute_path is not None
        ):
            raise ValueError("add intent requires a matching resource-block anchor")


@dataclass(frozen=True)
class RemoveAttribute:
    address: OpenTofuAddress
    anchor: SourceAnchor

    def __post_init__(self) -> None:
        if self.anchor.address != self.address or self.anchor.attribute_path is None:
            raise ValueError("remove intent requires a matching attribute anchor")


@dataclass(frozen=True)
class DeleteResource:
    address: OpenTofuAddress
    anchor: SourceAnchor


@dataclass(frozen=True)
class AppendResource:
    address: OpenTofuAddress
    resource: FrozenObject


@dataclass(frozen=True)
class AppendImport:
    address: OpenTofuAddress
    import_id: str


@dataclass(frozen=True)
class DeclareVariable:
    name: str
    declaration: FrozenObject


EditIntent: TypeAlias = (
    UpdateScalar
    | AddAttribute
    | RemoveAttribute
    | DeleteResource
    | AppendResource
    | AppendImport
    | DeclareVariable
)


@dataclass(frozen=True)
class ResourceDecision:
    address: OpenTofuAddress
    disposition: Disposition
    reason: ReasonCode
    edits: tuple[EditIntent, ...]
    messages: tuple[str, ...]


_BLOCKING_DISPOSITIONS = frozenset(
    {Disposition.CONFLICT, Disposition.ATTENTION, Disposition.FORBIDDEN}
)


@dataclass(frozen=True)
class ReconcilePlan:
    decisions: tuple[ResourceDecision, ...]

    @property
    def blocked(self) -> bool:
        return any(item.disposition in _BLOCKING_DISPOSITIONS for item in self.decisions)

    @property
    def edits(self) -> tuple[EditIntent, ...]:
        if self.blocked:
            return ()
        return tuple(
            sorted(
                (edit for decision in self.decisions for edit in decision.edits),
                key=_edit_sort_key,
            )
        )


def _edit_sort_key(edit: EditIntent) -> tuple[str, str, str]:
    address = edit.address.absolute if hasattr(edit, "address") else ""
    return address, type(edit).__name__, repr(edit)


class InvalidSnapshot(UbitofuError):
    """The immutable snapshot contains a contradictory internal combination."""
