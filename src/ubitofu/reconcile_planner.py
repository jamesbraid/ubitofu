# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 James Braid
"""Total, deterministic planning over immutable reconciliation observations."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from .enumerator import derive_identity
from .manifest import spec_for_type
from .module_index import ModuleIndex
from .reconcile_model import (
    ActionVector,
    AddAttribute,
    AppendImport,
    AppendResource,
    DeleteImport,
    DeleteResource,
    Disposition,
    EditIntent,
    InvalidSnapshot,
    ReasonCode,
    ReconcilePlan,
    ReconcileSnapshot,
    RemoveAttribute,
    ResourceDecision,
    ResourceObservation,
    SecretChangeKind,
    SourceAnchor,
    SourceAttribute,
    UpdateScalar,
)
from .values import FrozenObject, FrozenValue


def build_reconcile_plan(snapshot: ReconcileSnapshot) -> ReconcilePlan:
    """Classify every valid observation without performing I/O."""
    addresses = [item.address for item in snapshot.resources]
    if len(addresses) != len(set(addresses)):
        raise InvalidSnapshot("snapshot contains duplicate observations")
    classified = tuple(
        _decide(item) for item in sorted(snapshot.resources, key=lambda item: item.address)
    )
    decisions = tuple(
        _decision_for_module(decision, snapshot.module) for decision in classified
    )
    return ReconcilePlan(decisions)


def _decision_for_module(
    decision: ResourceDecision, module: ModuleIndex
) -> ResourceDecision:
    if decision.disposition is not Disposition.REMOVE:
        return decision
    target = decision.address.absolute
    referenced = any(
        reference.kind == "ordinary"
        and _reference_targets(reference.target_address, target)
        for reference in module.references
    )
    if referenced:
        return ResourceDecision(
            decision.address,
            Disposition.ATTENTION,
            ReasonCode.DANGLING_REFERENCE,
            (),
            (),
        )
    imports = [item for item in module.imports if item.address == target]
    if not imports:
        return decision
    if len(imports) != 1:
        return ResourceDecision(
            decision.address,
            Disposition.ATTENTION,
            ReasonCode.SOURCE_OWNERSHIP_AMBIGUOUS,
            (),
            (),
        )
    owned = imports[0]
    if not owned.editable or owned.block is None:
        return ResourceDecision(
            decision.address,
            Disposition.ATTENTION,
            ReasonCode.JSON_SOURCE_READ_ONLY,
            (),
            (),
        )
    sources = [source for source in module.sources if source.relative_path == owned.source_path]
    if len(sources) != 1 or not sources[0].active:
        return ResourceDecision(
            decision.address,
            Disposition.ATTENTION,
            ReasonCode.SOURCE_OWNERSHIP_AMBIGUOUS,
            (),
            (),
        )
    span = owned.block.whole
    expected = sources[0].source[span.start : span.end]
    if not expected:
        return ResourceDecision(
            decision.address,
            Disposition.ATTENTION,
            ReasonCode.SOURCE_OWNERSHIP_AMBIGUOUS,
            (),
            (),
        )
    return ResourceDecision(
        decision.address,
        decision.disposition,
        decision.reason,
        (*decision.edits, DeleteImport(decision.address, owned.source_path, expected)),
        decision.messages,
    )


def _reference_targets(reference: str, address: str) -> bool:
    return reference == address or reference.startswith((f"{address}.", f"{address}["))


def _decide(observation: ResourceObservation) -> ResourceDecision:
    address = observation.address
    change = observation.change
    if change is not None and change.address != address:
        raise InvalidSnapshot("resource change address does not match observation")
    if observation.blocking_reasons:
        reason = min(observation.blocking_reasons, key=lambda item: item.value)
        return _decision(observation, Disposition.ATTENTION, reason)
    if (
        address.module is not None
        or address.index is not None
        or address.mode != "managed"
        or address.deposed
    ):
        return _decision(observation, Disposition.ATTENTION, ReasonCode.UNSUPPORTED_ADDRESS)
    if change is not None and change.action in {
        ActionVector.DELETE_CREATE,
        ActionVector.CREATE_DELETE,
    }:
        return _decision(
            observation, Disposition.ATTENTION, ReasonCode.REPLACEMENT_REQUIRES_ATTENTION
        )

    existence = _existence_decision(observation)
    if existence is not None:
        return existence
    return _value_decision(observation)


def _existence_decision(observation: ResourceObservation) -> ResourceDecision | None:
    committed = observation.committed is not None
    base = observation.base
    desired = observation.desired
    live = observation.live
    action = observation.change.action if observation.change is not None else None

    if (
        not committed
        and base is None
        and desired is None
        and live is None
        and observation.fresh_present is True
        and observation.fresh is not None
    ):
        if observation.import_id is None:
            return _decision(
                observation,
                Disposition.ATTENTION,
                ReasonCode.INCOMPARABLE_CONTROLLER_OBSERVATION,
            )
        edits = (
            AppendResource(observation.address, observation.fresh),
            AppendImport(observation.address, observation.import_id),
        )
        return _editable_decision(
            observation, Disposition.APPEND, ReasonCode.LIVE_RESOURCE_NEW, edits
        )

    if observation.lifecycle.create_in_ui_only and base is None and action is ActionVector.CREATE:
        return _decision(
            observation, Disposition.FORBIDDEN, ReasonCode.FORBIDDEN_DEVICE_CREATE
        )
    if base is not None and desired is not None and live is None and action is ActionVector.CREATE:
        if observation.lifecycle.deletion_policy == "capture" and committed:
            assert observation.committed is not None
            edit = DeleteResource(
                observation.address,
                SourceAnchor(observation.address, None, observation.committed.block_bytes),
            )
            return _editable_decision(
                observation,
                Disposition.REMOVE,
                ReasonCode.CONTROLLER_RESOURCE_DELETED,
                (edit,),
            )
        if observation.lifecycle.deletion_policy == "forbid":
            return _decision(
                observation, Disposition.FORBIDDEN, ReasonCode.CONTROLLER_RESOURCE_DELETED
            )
        return _decision(
            observation, Disposition.ATTENTION, ReasonCode.CONTROLLER_RESOURCE_DELETED
        )
    if not committed and base is None and desired is not None and live is not None:
        import_id = _import_identity(observation)
        if import_id is None:
            return _decision(
                observation,
                Disposition.ATTENTION,
                ReasonCode.INCOMPARABLE_CONTROLLER_OBSERVATION,
            )
        edits = (
            AppendResource(observation.address, desired),
            AppendImport(observation.address, import_id),
        )
        return _editable_decision(
            observation, Disposition.APPEND, ReasonCode.LIVE_RESOURCE_NEW, edits
        )
    if committed and base is None and action is ActionVector.CREATE:
        return _decision(observation, Disposition.PRESERVE_CODE, ReasonCode.PENDING_CREATE)
    if not committed and base is not None and desired is None and action in {
        ActionVector.DELETE,
        ActionVector.FORGET,
    }:
        if live != base:
            return _decision(
                observation, Disposition.CONFLICT, ReasonCode.DELETE_MODIFY_CONFLICT
            )
        reason = (
            ReasonCode.PENDING_DELETE
            if action is ActionVector.DELETE
            else ReasonCode.PENDING_FORGET
        )
        return _decision(observation, Disposition.PRESERVE_CODE, reason)
    if not committed and base is not None and desired is None:
        return _decision(observation, Disposition.ATTENTION, ReasonCode.STATE_ORPHANED)
    if committed and base is None and action is None:
        return _decision(
            observation, Disposition.ATTENTION, ReasonCode.COMMITTED_NOT_IN_STATE
        )
    if base is None or desired is None or live is None:
        raise InvalidSnapshot("resource existence combination has no defined provenance")
    return None


def _value_decision(observation: ResourceObservation) -> ResourceDecision:
    assert observation.base is not None
    assert observation.desired is not None
    assert observation.live is not None
    comparable_kinds = {
        fact.kind for fact in observation.secret_changes if fact.comparable
    }
    incomparable_kinds = {
        fact.kind for fact in observation.secret_changes if not fact.comparable
    }
    if incomparable_kinds.intersection(
        {SecretChangeKind.CONFLICT, SecretChangeKind.LIVE_ONLY}
    ):
        return _decision(
            observation,
            Disposition.ATTENTION,
            ReasonCode.INCOMPARABLE_SECRET_OBSERVATION,
        )
    if SecretChangeKind.CONFLICT in comparable_kinds:
        return _decision(
            observation, Disposition.CONFLICT, ReasonCode.CONCURRENT_SECRET_CONFLICT
        )
    if SecretChangeKind.LIVE_ONLY in comparable_kinds:
        return _decision(
            observation,
            Disposition.ATTENTION,
            ReasonCode.LIVE_SECRET_CHANGE_UNCAPTURABLE,
        )
    base = _thaw(observation.base)
    desired = _thaw(observation.desired)
    live = _thaw(observation.live)
    unknown_paths = _truthy_paths(
        observation.change.after_unknown if observation.change is not None else FrozenObject(())
    )
    secret_paths = _secret_paths(base) | _secret_paths(desired) | _secret_paths(live)
    for path in unknown_paths | secret_paths:
        _remove_path(base, path)
        _remove_path(desired, path)
        _remove_path(live, path)
    result = _MergeResult()
    identities = {
        policy.attribute_path: policy.identity_attribute
        for policy in observation.collection_identities
    }
    _merge(base, desired, live, (), identities, result, allow_object=True)
    if result.unstable:
        return _decision(
            observation, Disposition.ATTENTION, ReasonCode.UNSTABLE_COLLECTION_IDENTITY
        )
    if result.conflict:
        return _decision(
            observation, Disposition.CONFLICT, ReasonCode.CONCURRENT_VALUE_CONFLICT
        )
    if result.captures:
        if (
            observation.committed is not None
            and observation.committed.file.relative_path.name.endswith(
                (".tf.json", ".tofu.json")
            )
        ):
            return _decision(
                observation, Disposition.ATTENTION, ReasonCode.JSON_SOURCE_READ_ONLY
            )
        edits = _capture_edits(observation, result.captures)
        if edits is None:
            return _decision(
                observation,
                Disposition.ATTENTION,
                ReasonCode.SOURCE_OWNERSHIP_AMBIGUOUS,
            )
        return _editable_decision(
            observation, Disposition.CAPTURE_LIVE, ReasonCode.LIVE_ONLY_CHANGE, edits
        )
    if result.code:
        return _decision(observation, Disposition.PRESERVE_CODE, ReasonCode.CODE_ONLY_CHANGE)
    if result.converged:
        return _decision(
            observation,
            Disposition.NO_CHANGE,
            ReasonCode.CONCURRENT_CHANGE_CONVERGED,
        )
    if unknown_paths:
        return _decision(
            observation, Disposition.NO_CHANGE, ReasonCode.COMPUTED_OR_UNKNOWN
        )
    all_secret_kinds = comparable_kinds | incomparable_kinds
    if SecretChangeKind.CODE_ONLY in all_secret_kinds:
        return _decision(observation, Disposition.PRESERVE_CODE, ReasonCode.CODE_ONLY_CHANGE)
    if SecretChangeKind.CONVERGED in comparable_kinds:
        return _decision(
            observation,
            Disposition.NO_CHANGE,
            ReasonCode.CONCURRENT_CHANGE_CONVERGED,
        )
    if secret_paths or observation.secret_changes:
        return _decision(observation, Disposition.NO_CHANGE, ReasonCode.SECRET_SUPPRESSED)
    return _decision(observation, Disposition.NO_CHANGE, ReasonCode.NO_CHANGE)


def _capture_edits(
    observation: ResourceObservation,
    captures: list[tuple[tuple[str | int, ...], object, object]],
) -> tuple[EditIntent, ...] | None:
    source = observation.committed
    if source is None:
        return None
    by_path: dict[tuple[str | int, ...], list[SourceAttribute]] = {}
    for attribute in source.source_attributes:
        by_path.setdefault(attribute.attribute_path, []).append(attribute)
    edits: list[EditIntent] = []
    for path, expected, replacement in sorted(captures, key=lambda item: item[0]):
        owned = by_path.get(path, [])
        if expected is _ABSENT:
            if owned or not source.block_bytes or len(path) != 1 or not isinstance(path[0], str):
                return None
            edits.append(
                AddAttribute(
                    observation.address,
                    path,
                    SourceAnchor(observation.address, None, source.block_bytes),
                    _literal(replacement),
                )
            )
            continue
        if not owned:
            containing = [
                attribute
                for attribute in source.source_attributes
                if path[: len(attribute.attribute_path)] == attribute.attribute_path
            ]
            if expected is not _ABSENT and replacement is not _ABSENT:
                owned = containing
        if len(owned) != 1:
            return None
        attribute = owned[0]
        if not attribute.literal:
            return None
        anchor = SourceAnchor(
            observation.address,
            path,
            attribute.expression_bytes,
        )
        if replacement is _ABSENT:
            edits.append(RemoveAttribute(observation.address, anchor))
        else:
            edits.append(
                UpdateScalar(
                    observation.address,
                    anchor,
                    _literal(replacement),
                )
            )
    return tuple(edits)


@dataclass
class _MergeResult:
    captures: list[tuple[tuple[str | int, ...], object, object]] = field(default_factory=list)
    code: bool = False
    converged: bool = False
    conflict: bool = False
    unstable: bool = False


_ABSENT = object()


def _merge(
    base: object,
    desired: object,
    live: object,
    path: tuple[str | int, ...],
    identities: dict[tuple[str | int, ...], str],
    result: _MergeResult,
    *,
    allow_object: bool,
) -> None:
    if base == desired == live:
        return
    identity = identities.get(path)
    if identity is not None:
        base_keyed = _keyed(base, identity)
        desired_keyed = _keyed(desired, identity)
        live_keyed = _keyed(live, identity)
        if base_keyed is None or desired_keyed is None or live_keyed is None:
            result.unstable = True
            return
        for key in sorted(
            set(base_keyed) | set(desired_keyed) | set(live_keyed), key=repr
        ):
            _merge(
                base_keyed.get(key, _ABSENT),
                desired_keyed.get(key, _ABSENT),
                live_keyed.get(key, _ABSENT),
                (*path, key),
                identities,
                result,
                allow_object=True,
            )
        return
    if (
        allow_object
        and isinstance(base, dict)
        and isinstance(desired, dict)
        and isinstance(live, dict)
    ):
        for key in sorted(set(base) | set(desired) | set(live)):
            _merge(
                base.get(key, _ABSENT),
                desired.get(key, _ABSENT),
                live.get(key, _ABSENT),
                (*path, key),
                identities,
                result,
                allow_object=False,
            )
        return
    if base == live and desired != base:
        result.code = True
        return
    if base == desired and live != base:
        result.captures.append((path, desired, live))
        return
    if desired == live and base != desired:
        result.converged = True
        return
    result.conflict = True


def _keyed(value: object, identity: str) -> dict[str | int, object] | None:
    if value is _ABSENT:
        return {}
    if isinstance(value, dict):
        entries = list(value.values())
    elif isinstance(value, list):
        entries = value
    else:
        return None
    keyed: dict[str | int, object] = {}
    for item in entries:
        if not isinstance(item, dict) or identity not in item:
            return None
        key = item[identity]
        if not isinstance(key, str | int) or isinstance(key, bool) or key in keyed:
            return None
        keyed[key] = item
    return keyed


def _editable_decision(
    observation: ResourceObservation,
    disposition: Disposition,
    reason: ReasonCode,
    edits: tuple[EditIntent, ...],
) -> ResourceDecision:
    if observation.committed is not None and observation.committed.file.relative_path.name.endswith(
        (".tf.json", ".tofu.json")
    ):
        return _decision(observation, Disposition.ATTENTION, ReasonCode.JSON_SOURCE_READ_ONLY)
    return ResourceDecision(observation.address, disposition, reason, edits, ())


def _decision(
    observation: ResourceObservation, disposition: Disposition, reason: ReasonCode
) -> ResourceDecision:
    return ResourceDecision(observation.address, disposition, reason, (), ())


def _import_identity(observation: ResourceObservation) -> str | None:
    values = observation.live or observation.desired
    if values is None:
        return None
    try:
        spec = spec_for_type(observation.address.resource_type)
    except KeyError:
        return None
    raw = _thaw(values)
    if not isinstance(raw, dict):
        return None
    site = str(raw.get("site") or raw.get("id") or "")
    return derive_identity(spec.id_rule, raw, site)


def _thaw(value: FrozenValue) -> object:
    if isinstance(value, FrozenObject):
        return {key: _thaw(item) for key, item in value.items}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return value


def _truthy_paths(
    value: FrozenValue, prefix: tuple[str | int, ...] = ()
) -> set[tuple[str | int, ...]]:
    if isinstance(value, FrozenObject):
        return {
            path
            for key, item in value.items
            for path in _truthy_paths(item, (*prefix, key))
        }
    if isinstance(value, tuple):
        return {
            path
            for index, item in enumerate(value)
            for path in _truthy_paths(item, (*prefix, index))
        }
    return {prefix} if value is True else set()


_SECRET_NAME = re.compile(r"private_key|passphrase|secret|token|password", re.IGNORECASE)


def _secret_paths(value: object, prefix: tuple[str | int, ...] = ()) -> set[tuple[str | int, ...]]:
    if isinstance(value, dict):
        found: set[tuple[str | int, ...]] = set()
        for key, item in value.items():
            path = (*prefix, key)
            if "public" not in key.lower() and _SECRET_NAME.search(key):
                found.add(path)
            else:
                found.update(_secret_paths(item, path))
        return found
    if isinstance(value, list):
        return {
            path
            for index, item in enumerate(value)
            for path in _secret_paths(item, (*prefix, index))
        }
    return set()


def _remove_path(value: object, path: tuple[str | int, ...]) -> None:
    if not path:
        return
    current = value
    for part in path[:-1]:
        if isinstance(part, str) and isinstance(current, dict):
            current = current.get(part)
        elif isinstance(part, int) and isinstance(current, list) and part < len(current):
            current = current[part]
        else:
            return
    last = path[-1]
    if isinstance(last, str) and isinstance(current, dict):
        current.pop(last, None)
    elif isinstance(last, int) and isinstance(current, list) and last < len(current):
        current[last] = None


def _literal(value: object) -> bytes:
    if value is _ABSENT:
        raise ValueError("absence has no HCL literal")
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
