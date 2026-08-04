# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 James Braid
"""Typed interpretation of read-only UniFi health observations."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass

from .controller import Controller
from .errors import ControllerResponseError, UbitofuError
from .outcomes import (
    HEALTH_RANKS,
    CommandOutcome,
    OutcomeItem,
    decode_receipt,
    opaque_reference,
)
from .values import FrozenObject, FrozenValue, freeze_value

HEALTH_ENDPOINT = "stat/health"
_OPAQUE_REFERENCE = re.compile(r"^ref-[0-9a-f]{64}$")
_RAW_SUBSYSTEM = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")
_HEALTH_DIGEST_DOMAIN = b"dev.ubitofu.health-snapshot.v1"


@dataclass(frozen=True, order=True)
class SubsystemHealth:
    subsystem: str
    status: str
    rank: int

    def __post_init__(self) -> None:
        if (
            _OPAQUE_REFERENCE.fullmatch(self.subsystem) is None
            or not isinstance(self.rank, int)
            or isinstance(self.rank, bool)
            or HEALTH_RANKS.get(self.status) != self.rank
        ):
            raise ValueError("invalid subsystem health")


@dataclass(frozen=True)
class HealthSnapshot:
    subsystems: tuple[SubsystemHealth, ...]

    def __post_init__(self) -> None:
        if not all(isinstance(item, SubsystemHealth) for item in self.subsystems):
            raise ValueError("invalid health snapshot")
        ordered = tuple(sorted(self.subsystems))
        if len({item.subsystem for item in ordered}) != len(ordered):
            raise ValueError("duplicate health subsystem")
        object.__setattr__(self, "subsystems", ordered)

    @property
    def sha256(self) -> str:
        rows = [
            {"rank": item.rank, "status": item.status, "subsystem": item.subsystem}
            for item in self.subsystems
        ]
        raw = json.dumps(rows, sort_keys=True, separators=(",", ":")).encode("ascii")
        digest = hashlib.sha256()
        digest.update(len(_HEALTH_DIGEST_DOMAIN).to_bytes(8, "big"))
        digest.update(_HEALTH_DIGEST_DOMAIN)
        digest.update(len(raw).to_bytes(8, "big"))
        digest.update(raw)
        return digest.hexdigest()


def capture_health(controller: Controller) -> HealthSnapshot:
    """Read the health endpoint once and normalize only policy-owned fields."""
    try:
        records = controller.collection(HEALTH_ENDPOINT)
        if not records:
            raise ValueError("empty health document")
        subsystems = tuple(_parse_subsystem(record) for record in records)
        return HealthSnapshot(subsystems)
    except ControllerResponseError as exc:
        raise _health_controller_error(exc) from exc
    except (TypeError, ValueError) as exc:
        raise _health_controller_error(
            ControllerResponseError(HEALTH_ENDPOINT, 200, "invalid document")
        ) from exc


def _health_controller_error(error: ControllerResponseError) -> ControllerResponseError:
    return ControllerResponseError(
        opaque_reference(f"health-endpoint:{HEALTH_ENDPOINT}"),
        error.status,
        error.reason,
    )


def _parse_subsystem(record: dict[str, object]) -> SubsystemHealth:
    subsystem = record.get("subsystem")
    status = record.get("status")
    if (
        not isinstance(subsystem, str)
        or _RAW_SUBSYSTEM.fullmatch(subsystem) is None
        or subsystem != subsystem.strip()
        or not isinstance(status, str)
    ):
        raise ValueError("missing health field")
    rank = HEALTH_RANKS.get(status)
    if rank is None:
        raise ValueError("unknown health status")
    return SubsystemHealth(opaque_reference(f"health-subsystem:{subsystem}"), status, rank)


def health_snapshot_outcome(snapshot: HealthSnapshot) -> CommandOutcome:
    """Build the closed public receipt for one successfully captured snapshot."""
    if not snapshot.subsystems:
        raise ValueError("health snapshot cannot be empty")
    return CommandOutcome(
        command="health_snapshot",
        changed=False,
        blocked=False,
        summary="health snapshot complete",
        items=(
            OutcomeItem(
                "health_snapshot_captured", "info", None, "health snapshot captured"
            ),
        ),
        input_digests=(("controller", snapshot.sha256),),
        payload=_snapshot_payload(snapshot),
    )


def compare_health(before: HealthSnapshot, after: HealthSnapshot) -> CommandOutcome:
    """Classify only health changes relative to the supplied baseline."""
    before_by_ref = {item.subsystem: item for item in before.subsystems}
    after_by_ref = {item.subsystem: item for item in after.subsystems}
    items = tuple(
        _comparison_item(reference, before_by_ref.get(reference), after_by_ref.get(reference))
        for reference in sorted(before_by_ref.keys() | after_by_ref.keys())
    )
    blocked = any(item.severity == "blocking" for item in items)
    return CommandOutcome(
        command="health_compare",
        changed=before != after,
        blocked=blocked,
        summary="health comparison complete",
        items=items,
        input_digests=(("health_before", before.sha256), ("health_after", after.sha256)),
        payload=_snapshot_payload(after),
    )


def _comparison_item(
    reference: str,
    before: SubsystemHealth | None,
    after: SubsystemHealth | None,
) -> OutcomeItem:
    if after is None:
        return OutcomeItem(
            "health_subsystem_missing",
            "blocking",
            reference,
            "health subsystem is missing",
        )
    if after.status == "unknown" and (before is None or before.status != "unknown"):
        return OutcomeItem(
            "health_unknown_new", "blocking", reference, "new health status is unknown"
        )
    if before is None:
        if after.rank == 0:
            return OutcomeItem(
                "health_subsystem_added", "info", reference, "health subsystem added"
            )
        return OutcomeItem("health_degraded", "blocking", reference, "health degraded")
    if before.status == after.status:
        if after.status == "unknown":
            return OutcomeItem("advisory", "warning", reference, "operator attention advised")
        return OutcomeItem("health_unchanged", "info", reference, "health is unchanged")
    if after.rank > before.rank:
        return OutcomeItem("health_degraded", "blocking", reference, "health degraded")
    return OutcomeItem("health_recovered", "info", reference, "health recovered")


def health_snapshot_from_receipt(raw: bytes) -> HealthSnapshot:
    """Decode only a version-one health-snapshot receipt into comparison state."""
    envelope = decode_receipt(raw)
    expected_item = OutcomeItem(
        "health_snapshot_captured", "info", None, "health snapshot captured"
    )
    if (
        envelope.outcome.command != "health_snapshot"
        or envelope.outcome.changed
        or envelope.outcome.blocked
        or envelope.outcome.items != (expected_item,)
    ):
        raise UbitofuError("health baseline is unavailable")
    payload = envelope.outcome.payload
    if not isinstance(payload, FrozenObject):
        raise UbitofuError("health baseline is unavailable")
    values = dict(payload.items)
    rows = values.get("subsystems")
    if not isinstance(rows, tuple):
        raise UbitofuError("health baseline is unavailable")
    try:
        subsystems = tuple(_subsystem_from_payload(row) for row in rows)
        snapshot = HealthSnapshot(subsystems)
        if (
            not snapshot.subsystems
            or dict(envelope.outcome.input_digests).get("controller") != snapshot.sha256
        ):
            raise ValueError("health payload digest mismatch")
        return snapshot
    except (TypeError, ValueError) as exc:
        raise UbitofuError("health baseline is unavailable") from exc


def _subsystem_from_payload(value: object) -> SubsystemHealth:
    if not isinstance(value, FrozenObject):
        raise ValueError("invalid health payload")
    fields = dict(value.items)
    reference = fields.get("ref")
    status = fields.get("status")
    rank = fields.get("rank")
    if (
        not isinstance(reference, str)
        or not isinstance(status, str)
        or not isinstance(rank, int)
        or isinstance(rank, bool)
    ):
        raise ValueError("invalid health payload")
    return SubsystemHealth(reference, status, rank)


def _snapshot_payload(snapshot: HealthSnapshot) -> FrozenValue:
    return freeze_value(
        {
            "subsystems": [
                {"ref": item.subsystem, "status": item.status, "rank": item.rank}
                for item in snapshot.subsystems
            ]
        }
    )
