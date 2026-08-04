# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 James Braid
"""Stable command outcomes, receipts, and intentionally small output plumbing."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import IO, Literal, cast

from .errors import UbitofuError
from .values import FrozenObject, FrozenValue, freeze_value

_SCHEMA = "dev.ubitofu.receipt"
_VERSION = 1
_MAX_RECEIPT_BYTES = 128 * 1024
_MAX_RECEIPT_DEPTH = 16
_MAX_RECEIPT_VALUES = 4096
_MAX_RECEIPT_STRING = 240
_OPAQUE_REFERENCE = re.compile(r"^ref-[0-9a-f]{64}$")
_ITEM_VOCABULARY: dict[str, tuple[Literal["info", "warning", "blocking"], str]] = {
    "advisory": ("warning", "operator attention advised"),
    "captured_change": ("info", "captured controller changes"),
    "reconciliation_blocked": ("blocking", "reconciliation blocked"),
    "no_change": ("info", "no managed change"),
    "code_only_change": ("info", "preserved declared change"),
    "live_only_change": ("info", "captured controller changes"),
    "concurrent_change_converged": ("info", "concurrent changes converged"),
    "concurrent_value_conflict": ("blocking", "concurrent values conflict"),
    "computed_or_unknown": ("warning", "computed values require attention"),
    "secret_suppressed": ("warning", "secret values remain suppressed"),
    "concurrent_secret_conflict": ("blocking", "concurrent secret values conflict"),
    "live_secret_change_uncapturable": (
        "blocking",
        "controller secret change cannot be captured",
    ),
    "incomparable_secret_observation": (
        "blocking",
        "controller secret observation is incomparable",
    ),
    "secret_freshness_unverified": (
        "warning",
        "saved plan does not verify current secret values",
    ),
    "live_resource_new": ("info", "captured new controller resource"),
    "controller_resource_deleted": ("info", "captured controller resource deletion"),
    "pending_create": ("warning", "declared resource is pending create"),
    "pending_delete": ("warning", "declared resource is pending delete"),
    "pending_forget": ("warning", "declared resource is pending forget"),
    "state_orphaned": ("blocking", "state resource is orphaned"),
    "committed_not_in_state": ("blocking", "declared resource is absent from state"),
    "forbidden_device_create": ("blocking", "managed device creation is forbidden"),
    "unsupported_address": ("blocking", "resource address is unsupported"),
    "stale_controller_observation": ("blocking", "controller observation is stale"),
    "incomparable_controller_observation": ("blocking", "controller observation is incomparable"),
    "unstable_collection_identity": ("blocking", "collection identity is unstable"),
    "delete_modify_conflict": ("blocking", "delete conflicts with modification"),
    "replacement_requires_attention": ("blocking", "replacement requires attention"),
    "json_source_read_only": ("blocking", "JSON source is read only"),
    "dangling_reference": ("blocking", "deletion leaves a dangling reference"),
    "declared_complex_drift": ("blocking", "declared complex value drifted"),
    "source_ownership_ambiguous": ("blocking", "source ownership is ambiguous"),
    "plan_allowed": ("info", "saved plan is allowed"),
    "unsafe_plan": ("blocking", "saved plan is unsafe"),
    "generation_preview": ("info", "generation preview is ready"),
    "generation_blocked": ("blocking", "generation preview is blocked"),
    "coverage_gap": ("warning", "controller coverage is incomplete"),
    "unmapped_controller_resource": ("warning", "controller resource is unmapped"),
    "unsupported_endpoint": ("warning", "controller endpoint is unsupported"),
    "inspection_complete": ("info", "inspection is complete"),
    "health_snapshot_captured": ("info", "health snapshot captured"),
    "health_snapshot_failed": ("blocking", "health snapshot is unavailable"),
    "health_unchanged": ("info", "health is unchanged"),
    "health_recovered": ("info", "health recovered"),
    "health_degraded": ("blocking", "health degraded"),
    "health_unknown_new": ("blocking", "new health status is unknown"),
    "health_subsystem_added": ("info", "health subsystem added"),
    "health_subsystem_missing": ("blocking", "health subsystem is missing"),
    "health_baseline_missing": ("blocking", "health baseline is unavailable"),
}


@dataclass(frozen=True)
class _CommandProfile:
    summary: str
    items: Mapping[str, tuple[Literal["info", "warning", "blocking"], str]]
    digest_names: frozenset[str]
    payload_kind: Literal["none", "preview", "health"]


def _profile(
    summary: str,
    reasons: frozenset[str],
    digest_names: frozenset[str],
    payload_kind: Literal["none", "preview", "health"],
) -> _CommandProfile:
    return _CommandProfile(
        summary,
        MappingProxyType({reason: _ITEM_VOCABULARY[reason] for reason in reasons}),
        digest_names,
        payload_kind,
    )


_RECONCILE_REASONS = frozenset(
    {
        "advisory",
        "captured_change",
        "reconciliation_blocked",
        "no_change",
        "code_only_change",
        "live_only_change",
        "concurrent_change_converged",
        "concurrent_value_conflict",
        "computed_or_unknown",
        "secret_suppressed",
        "concurrent_secret_conflict",
        "live_secret_change_uncapturable",
        "incomparable_secret_observation",
        "secret_freshness_unverified",
        "live_resource_new",
        "controller_resource_deleted",
        "pending_create",
        "pending_delete",
        "pending_forget",
        "state_orphaned",
        "committed_not_in_state",
        "forbidden_device_create",
        "unsupported_address",
        "stale_controller_observation",
        "incomparable_controller_observation",
        "unstable_collection_identity",
        "delete_modify_conflict",
        "replacement_requires_attention",
        "json_source_read_only",
        "dangling_reference",
        "declared_complex_drift",
        "source_ownership_ambiguous",
    }
)
COMMAND_PROFILES: Mapping[str, _CommandProfile] = MappingProxyType(
    {
        "generate": _profile(
            "generation preview complete",
            frozenset({"generation_preview", "generation_blocked", "coverage_gap", "advisory"}),
            frozenset({"active_source", "controller", "provider_schema"}),
            "preview",
        ),
        "reconcile": _profile(
            "reconciliation complete",
            _RECONCILE_REASONS,
            frozenset({"active_source", "controller"}),
            "preview",
        ),
        "check": _profile(
            "saved plan check complete",
            (_RECONCILE_REASONS - frozenset({"captured_change", "reconciliation_blocked"}))
            | frozenset({"plan_allowed", "unsafe_plan"}),
            frozenset({"saved_plan", "active_source", "plan_time_live", "fresh_controller"}),
            "none",
        ),
        "inspect": _profile(
            "inspection complete",
            frozenset(
                {
                    "inspection_complete",
                    "coverage_gap",
                    "unmapped_controller_resource",
                    "unsupported_endpoint",
                    "advisory",
                }
            ),
            frozenset({"controller", "provider_schema"}),
            "none",
        ),
        "health_snapshot": _profile(
            "health snapshot complete",
            frozenset({"health_snapshot_captured", "health_snapshot_failed", "advisory"}),
            frozenset({"controller"}),
            "health",
        ),
        "health_compare": _profile(
            "health comparison complete",
            frozenset(
                {
                    "health_unchanged",
                    "health_recovered",
                    "health_degraded",
                    "health_unknown_new",
                    "health_subsystem_added",
                    "health_subsystem_missing",
                    "health_baseline_missing",
                    "advisory",
                }
            ),
            frozenset({"health_before", "health_after"}),
            "health",
        ),
    }
)
HEALTH_RANKS: Mapping[str, int] = MappingProxyType(
    {"ok": 0, "warning": 1, "error": 2, "unknown": 3}
)


@dataclass(frozen=True)
class _FileFacts:
    device: int
    inode: int
    mode: int
    uid: int
    size: int
    mtime_ns: int


@dataclass(frozen=True)
class _DirectoryFacts:
    device: int
    inode: int
    mode: int
    uid: int


@dataclass(frozen=True)
class _OutputTarget:
    destination: Path
    owner_uid: int
    destination_facts: _FileFacts | None
    parents: tuple[tuple[Path, _DirectoryFacts], ...]


@dataclass(frozen=True)
class OutcomeItem:
    """One bounded, public explanation of a completed command."""

    reason_code: str
    severity: Literal["info", "warning", "blocking"]
    address: str | None
    message: str

    def __post_init__(self) -> None:
        expected = _ITEM_VOCABULARY.get(self.reason_code)
        if expected is None or (self.severity, self.message) != expected:
            raise ValueError("unsupported public outcome item")
        _validate_reference(self.address)


@dataclass(frozen=True)
class CommandOutcome:
    """The sole, immutable source for both human and machine command results."""

    command: str
    changed: bool
    blocked: bool
    summary: str
    items: tuple[OutcomeItem, ...]
    input_digests: tuple[tuple[str, str], ...]
    payload: FrozenValue | None

    def __post_init__(self) -> None:
        if not isinstance(self.changed, bool) or not isinstance(self.blocked, bool):
            raise ValueError("outcome flags must be booleans")
        profile = _command_profile(self.command)
        if profile.summary != self.summary:
            raise ValueError("unsupported public outcome summary")
        if not all(isinstance(item, OutcomeItem) for item in self.items):
            raise ValueError("outcome items must be typed")
        if any(
            profile.items.get(item.reason_code) != (item.severity, item.message)
            for item in self.items
        ):
            raise ValueError("outcome item is not allowed for command")
        has_blocking_item = any(item.severity == "blocking" for item in self.items)
        if self.blocked != has_blocking_item:
            raise ValueError("outcome blocking flag disagrees with items")
        object.__setattr__(self, "items", tuple(sorted(self.items, key=_item_sort_key)))
        object.__setattr__(self, "input_digests", _normalise_digests(self.input_digests, profile))
        object.__setattr__(
            self, "payload", _validate_payload(self.command, profile, self.payload)
        )


@dataclass(frozen=True)
class ReceiptEnvelope:
    """Versioned public receipt envelope."""

    outcome: CommandOutcome
    schema: Literal["dev.ubitofu.receipt"] = "dev.ubitofu.receipt"
    version: Literal[1] = 1

    def __post_init__(self) -> None:
        if self.schema != _SCHEMA or self.version != _VERSION:
            raise ValueError("unsupported receipt envelope")


def exit_code(outcome: CommandOutcome) -> Literal[0, 3]:
    """Map an already-classified domain result to its only two domain exits."""
    return 3 if outcome.blocked else 0


def render_human(outcome: CommandOutcome) -> str:
    """Render one bounded outcome without inspecting source controller artifacts."""
    lines = [outcome.summary]
    for item in outcome.items:
        location = "" if item.address is None else f" {item.address}"
        lines.append(f"{item.severity} {item.reason_code}{location}: {item.message}")
    if _command_profile(outcome.command).payload_kind == "preview":
        changed_paths = _preview_changed_paths(outcome.payload)
        if changed_paths:
            lines.append("Changed paths:")
            lines.extend(f"  {path}" for path in changed_paths)
    return "\n".join(lines) + "\n"


def render_json(outcome: CommandOutcome) -> bytes:
    """Return canonical version-one receipt bytes."""
    document = _receipt_document(ReceiptEnvelope(outcome))
    _validate_receipt_shape(document)
    rendered = _canonical_json(document)
    if len(rendered) > _MAX_RECEIPT_BYTES:
        raise ValueError("receipt is too large")
    return rendered


def decode_receipt(raw: bytes) -> ReceiptEnvelope:
    """Decode a version-one receipt while ignoring future optional fields."""
    if not isinstance(raw, bytes) or not raw or len(raw) > _MAX_RECEIPT_BYTES:
        raise UbitofuError("invalid receipt")
    try:
        decoded = json.loads(
            raw,
            object_pairs_hook=_object_without_duplicates,
            parse_constant=_reject_json_constant,
        )
        _validate_receipt_shape(decoded)
    except (RecursionError, TypeError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise UbitofuError("invalid receipt") from exc
    if not isinstance(decoded, dict):
        raise UbitofuError("invalid receipt")
    if decoded.get("schema") != _SCHEMA or decoded.get("version") != _VERSION:
        raise UbitofuError("unsupported receipt")
    if "outcome" not in decoded:
        raise UbitofuError("invalid receipt")
    return ReceiptEnvelope(_decode_outcome(decoded["outcome"]))


def digest_active_source(files: Iterable[tuple[PurePosixPath, bytes]]) -> str:
    """Digest exact active-source paths and bytes with unambiguous framing."""
    entries: list[tuple[bytes, bytes]] = []
    for path, content in files:
        if not isinstance(path, PurePosixPath) or not isinstance(content, bytes):
            raise ValueError("invalid active source digest input")
        _validate_relative_path(path)
        encoded_path = path.as_posix().encode("utf-8")
        if any(existing_path == encoded_path for existing_path, _ in entries):
            raise ValueError("duplicate active source path")
        entries.append((encoded_path, content))
    return _framed_digest(b"dev.ubitofu.active-source.v1", sorted(entries))


def digest_controller_observations(
    observations: Iterable[tuple[str, FrozenValue]],
) -> str:
    """Digest normalized, non-secret observations with explicit value framing."""
    entries: list[tuple[bytes, bytes]] = []
    for address, values in observations:
        if not isinstance(address, str):
            raise ValueError("invalid controller digest address")
        encoded_address = address.encode("utf-8")
        if any(existing_address == encoded_address for existing_address, _ in entries):
            raise ValueError("duplicate controller observation address")
        entries.append((encoded_address, _canonical_json(_thaw(values))))
    return _framed_digest(b"dev.ubitofu.controller-observations.v1", sorted(entries))


def digest_provider_schema(resources: Iterable[tuple[str, FrozenObject]]) -> str:
    """Digest the immutable provider resource schemas consumed by a command."""
    entries: list[tuple[bytes, bytes]] = []
    for resource_type, schema in resources:
        if not isinstance(resource_type, str) or not isinstance(schema, FrozenObject):
            raise ValueError("invalid provider schema digest input")
        encoded_type = resource_type.encode("utf-8")
        if any(existing_type == encoded_type for existing_type, _ in entries):
            raise ValueError("duplicate provider schema resource")
        entries.append((encoded_type, _canonical_json(_thaw(schema))))
    return _framed_digest(b"dev.ubitofu.provider-schema.v1", sorted(entries))


def reconcile_outcome(preview: object) -> CommandOutcome:
    """Build one value-free public outcome from a typed reconciliation preview."""
    from .reconcile_renderer import ReconcilePreview  # noqa: PLC0415

    if not isinstance(preview, ReconcilePreview):
        raise TypeError("reconciliation preview must be typed")
    if not preview.valid:
        raise ValueError("reconciliation preview is invalid")
    decisions = tuple(
        decision
        for decision in preview.plan.decisions
        if decision.reason.value != "no_change"
    )
    items = [
        OutcomeItem(
            decision.reason.value,
            _ITEM_VOCABULARY[decision.reason.value][0],
            opaque_reference(f"reconcile-address:{decision.address.absolute}"),
            _ITEM_VOCABULARY[decision.reason.value][1],
        )
        for decision in decisions
    ]
    if len(decisions) != len(preview.plan.decisions):
        items.append(OutcomeItem("no_change", "info", None, "no managed change"))
    if preview.plan.blocked:
        items.append(
            OutcomeItem(
                "reconciliation_blocked",
                "blocking",
                None,
                "reconciliation blocked",
            )
        )
    active_sources = tuple(
        (source.relative_path, source.source)
        for source in preview.snapshot.module.sources
        if source.active
    )
    return CommandOutcome(
        command="reconcile",
        changed=bool(preview.changed_paths),
        blocked=preview.plan.blocked,
        summary="reconciliation complete",
        items=tuple(items),
        input_digests=(
            ("active_source", digest_active_source(active_sources)),
            ("controller", preview.snapshot.controller_digest),
        ),
        payload=freeze_value(
            {
                "changed_paths": [path.as_posix() for path in preview.changed_paths],
                "candidate_digests": [
                    [path.as_posix(), digest]
                    for path, digest in preview.candidate_digests
                ],
            }
        ),
    )


def emit_output(
    outcome: CommandOutcome,
    *,
    format: Literal["human", "json"],
    output: str,
    stdout: IO[str],
) -> None:
    """Emit one rendered outcome to stdout or one private, atomic receipt file."""
    if format == "human":
        rendered = render_human(outcome)
    elif format == "json":
        rendered = render_json(outcome).decode("ascii")
    else:
        raise ValueError("unsupported output format")
    if output == "-":
        try:
            written = stdout.write(rendered)
        except OSError as exc:
            raise UbitofuError("receipt output write failed") from exc
        if written != len(rendered):
            raise UbitofuError("receipt output write failed")
        return
    _write_receipt_file(Path(output), rendered.encode("utf-8"))


def read_receipt_file(path: Path, *, owner_root: Path) -> bytes:
    """Read one bounded private receipt without following special files."""
    destination = path if path.is_absolute() else Path.cwd() / path
    owner = owner_root.resolve(strict=True).lstat()
    if stat.S_ISLNK(owner.st_mode) or not stat.S_ISDIR(owner.st_mode):
        raise UbitofuError("unsafe receipt input")
    parents = _capture_output_parents(destination.parent)
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    try:
        fd = os.open(destination, flags)
    except OSError as exc:
        raise UbitofuError("unsafe receipt input") from exc
    try:
        before = _file_facts(os.fstat(fd))
        if (
            not stat.S_ISREG(before.mode)
            or before.uid != owner.st_uid
            or stat.S_IMODE(before.mode) != 0o600
            or before.size <= 0
            or before.size > _MAX_RECEIPT_BYTES
        ):
            raise UbitofuError("unsafe receipt input")
        chunks: list[bytes] = []
        remaining = _MAX_RECEIPT_BYTES + 1
        while remaining:
            chunk = os.read(fd, min(64 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        content = b"".join(chunks)
        after = _file_facts(os.fstat(fd))
    except OSError as exc:
        raise UbitofuError("unsafe receipt input") from exc
    finally:
        os.close(fd)
    try:
        reopened = _file_facts(os.lstat(destination))
    except OSError as exc:
        raise UbitofuError("unsafe receipt input") from exc
    if before != after or before != reopened or len(content) != before.size:
        raise UbitofuError("unsafe receipt input")
    for parent, expected in parents:
        try:
            actual = _directory_facts(os.lstat(parent))
        except OSError as exc:
            raise UbitofuError("unsafe receipt input") from exc
        if actual != expected:
            raise UbitofuError("unsafe receipt input")
    return content


def _receipt_document(envelope: ReceiptEnvelope) -> dict[str, object]:
    outcome = envelope.outcome
    return {
        "outcome": {
            "blocked": outcome.blocked,
            "changed": outcome.changed,
            "command": outcome.command,
            "input_digests": [list(item) for item in outcome.input_digests],
            "items": [
                {
                    "address": item.address,
                    "message": item.message,
                    "reason_code": item.reason_code,
                    "severity": item.severity,
                }
                for item in outcome.items
            ],
            "payload": None if outcome.payload is None else _thaw(outcome.payload),
            "summary": outcome.summary,
        },
        "schema": envelope.schema,
        "version": envelope.version,
    }


def _decode_outcome(value: object) -> CommandOutcome:
    if not isinstance(value, dict):
        raise UbitofuError("invalid receipt")
    required = {"command", "changed", "blocked", "items", "input_digests", "payload"}
    if not required.issubset(value):
        raise UbitofuError("invalid receipt")
    command = value["command"]
    changed = value["changed"]
    blocked = value["blocked"]
    items_value = value["items"]
    digests_value = value["input_digests"]
    payload = value["payload"]
    if (
        not isinstance(command, str)
        or not isinstance(changed, bool)
        or not isinstance(blocked, bool)
        or not isinstance(items_value, list)
        or not isinstance(digests_value, list)
    ):
        raise UbitofuError("invalid receipt")
    try:
        items = tuple(_decode_item(item) for item in items_value)
        digests = tuple(_decode_digest(item) for item in digests_value)
        frozen_payload = None if payload is None else freeze_value(payload)
        summary = value.get("summary", _command_profile(command).summary)
        if not isinstance(summary, str):
            raise ValueError("invalid summary")
        return CommandOutcome(command, changed, blocked, summary, items, digests, frozen_payload)
    except (TypeError, ValueError) as exc:
        raise UbitofuError("invalid receipt") from exc


def _decode_item(value: object) -> OutcomeItem:
    if not isinstance(value, dict):
        raise ValueError("invalid item")
    required = {"reason_code", "severity", "address", "message"}
    if not required.issubset(value):
        raise ValueError("invalid item")
    reason = value["reason_code"]
    severity = value["severity"]
    address = value["address"]
    message = value["message"]
    if (
        not isinstance(reason, str)
        or not isinstance(severity, str)
        or address is not None
        and not isinstance(address, str)
        or not isinstance(message, str)
    ):
        raise ValueError("invalid item")
    return OutcomeItem(
        reason,
        cast(Literal["info", "warning", "blocking"], severity),
        address,
        message,
    )


def _decode_digest(value: object) -> tuple[str, str]:
    if (
        not isinstance(value, list)
        or len(value) != 2
        or not isinstance(value[0], str)
        or not isinstance(value[1], str)
    ):
        raise ValueError("invalid digest")
    return value[0], value[1]


def _normalise_digests(
    values: tuple[tuple[str, str], ...], profile: _CommandProfile
) -> tuple[tuple[str, str], ...]:
    normalised: list[tuple[str, str]] = []
    for name, digest in values:
        if not isinstance(name, str) or not isinstance(digest, str):
            raise ValueError("invalid input digest")
        if name not in profile.digest_names:
            raise ValueError("invalid input digest")
        if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
            raise ValueError("invalid input digest")
        normalised.append((name, digest))
    sorted_values = tuple(sorted(normalised))
    if (
        len(sorted_values) != len(profile.digest_names)
        or {name for name, _ in sorted_values} != profile.digest_names
    ):
        raise ValueError("duplicate input digest")
    return sorted_values


def _item_sort_key(item: OutcomeItem) -> tuple[str, str, str, str]:
    return item.reason_code, item.address or "", item.severity, item.message


def opaque_reference(identity: str | bytes) -> str:
    """Return an opaque public reference for one raw resource or subsystem identity."""
    if isinstance(identity, str):
        raw = identity.encode("utf-8")
    elif isinstance(identity, bytes):
        raw = identity
    else:
        raise ValueError("invalid opaque reference identity")
    if not raw:
        raise ValueError("invalid opaque reference identity")
    return "ref-" + _framed_digest(b"dev.ubitofu.public-reference.v1", [(b"identity", raw)])


def _command_profile(command: str) -> _CommandProfile:
    profile = COMMAND_PROFILES.get(command)
    if profile is None:
        raise ValueError("unsupported public outcome command")
    return profile


def _validate_reference(value: str | None) -> None:
    if value is None:
        return
    if not isinstance(value, str) or _OPAQUE_REFERENCE.fullmatch(value) is None:
        raise ValueError("invalid public outcome reference")


def _validate_payload(
    command: str, profile: _CommandProfile, payload: FrozenValue | None
) -> FrozenValue | None:
    if profile.payload_kind == "none":
        if payload is not None:
            raise ValueError("unsupported public outcome payload")
        return None
    if payload is None or not isinstance(payload, FrozenObject):
        raise ValueError("unsupported public outcome payload")
    if profile.payload_kind == "preview":
        return _validate_preview_payload(payload, command=command)
    return _validate_health_payload(payload)


def _validate_preview_payload(payload: FrozenObject, *, command: str) -> FrozenValue:
    values = dict(payload.items)
    if set(values) != {"changed_paths", "candidate_digests"} or len(values) != 2:
        raise ValueError("unsupported public outcome payload")
    paths = values["changed_paths"]
    if not isinstance(paths, tuple):
        raise ValueError("unsupported public outcome payload")
    canonical_paths: list[str] = []
    for path in paths:
        if not isinstance(path, str):
            raise ValueError("unsupported public outcome payload")
        relative = PurePosixPath(path)
        _validate_relative_path(relative)
        hcl_path = path.endswith((".tf", ".tofu", ".tf.json"))
        coverage_path = command == "generate" and path == "COVERAGE.md"
        if relative.as_posix() != path or not (hcl_path or coverage_path):
            raise ValueError("unsupported public outcome payload")
        canonical_paths.append(path)
    if len(set(canonical_paths)) != len(canonical_paths):
        raise ValueError("duplicate changed path")
    candidate_values = values["candidate_digests"]
    if not isinstance(candidate_values, tuple):
        raise ValueError("unsupported public outcome payload")
    candidates: list[tuple[str, str | None]] = []
    for candidate in candidate_values:
        if not isinstance(candidate, tuple) or len(candidate) != 2:
            raise ValueError("unsupported public outcome payload")
        path, digest = candidate
        if not isinstance(path, str) or digest is not None and not isinstance(digest, str):
            raise ValueError("unsupported public outcome payload")
        if path not in canonical_paths or digest is not None and not _is_digest(digest):
            raise ValueError("unsupported public outcome payload")
        candidates.append((path, digest))
    if len({path for path, _ in candidates}) != len(candidates):
        raise ValueError("duplicate candidate digest")
    if {path for path, _ in candidates} != set(canonical_paths):
        raise ValueError("candidate paths do not match changed paths")
    return freeze_value(
        {
            "changed_paths": sorted(canonical_paths),
            "candidate_digests": [list(candidate) for candidate in sorted(candidates)],
        }
    )


def _is_digest(value: str) -> bool:
    return len(value) == 64 and all(character in "0123456789abcdef" for character in value)


def _validate_health_payload(payload: FrozenObject) -> FrozenValue:
    values = dict(payload.items)
    if set(values) != {"subsystems"} or len(values) != 1:
        raise ValueError("unsupported public outcome payload")
    subsystems = values["subsystems"]
    if not isinstance(subsystems, tuple):
        raise ValueError("unsupported public outcome payload")
    normalized: list[tuple[str, str, int]] = []
    for subsystem in subsystems:
        if not isinstance(subsystem, FrozenObject):
            raise ValueError("unsupported public outcome payload")
        fields = dict(subsystem.items)
        if set(fields) != {"ref", "status", "rank"} or len(fields) != 3:
            raise ValueError("unsupported public outcome payload")
        reference = fields["ref"]
        status = fields["status"]
        rank = fields["rank"]
        if not isinstance(reference, str):
            raise ValueError("unsupported public outcome payload")
        _validate_reference(reference)
        if not isinstance(status, str) or not isinstance(rank, int) or isinstance(rank, bool):
            raise ValueError("unsupported public outcome payload")
        if HEALTH_RANKS.get(status) != rank:
            raise ValueError("unsupported public outcome payload")
        normalized.append((reference, status, rank))
    if len({reference for reference, _, _ in normalized}) != len(normalized):
        raise ValueError("duplicate health subsystem")
    return freeze_value(
        {
            "subsystems": [
                {"ref": reference, "status": status, "rank": rank}
                for reference, status, rank in sorted(normalized)
            ]
        }
    )


def _preview_changed_paths(payload: FrozenValue | None) -> tuple[str, ...]:
    """Return the already-validated, canonical paths in a preview payload."""
    if not isinstance(payload, FrozenObject):
        raise ValueError("unsupported public outcome payload")
    values = dict(payload.items)
    changed_paths = values.get("changed_paths")
    if not isinstance(changed_paths, tuple) or not all(
        isinstance(path, str) for path in changed_paths
    ):
        raise ValueError("unsupported public outcome payload")
    return cast(tuple[str, ...], changed_paths)


def _thaw(value: FrozenValue) -> object:
    if value is None or isinstance(value, bool | int | float | str):
        return value
    if isinstance(value, FrozenObject):
        return {key: _thaw(item) for key, item in value.items}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    raise ValueError("unsupported frozen value")


def _canonical_json(value: object) -> bytes:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    )
    return encoded.encode("ascii") + b"\n"


def _object_without_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
    document: dict[str, object] = {}
    for key, value in pairs:
        if key in document:
            raise ValueError("duplicate JSON object key")
        document[key] = value
    return document


def _reject_json_constant(value: str) -> object:
    raise ValueError(f"unsupported JSON constant: {value}")


def _framed_digest(domain: bytes, entries: list[tuple[bytes, bytes]]) -> str:
    digest = hashlib.sha256()
    _frame(digest, domain)
    for key, value in entries:
        _frame(digest, key)
        _frame(digest, value)
    return digest.hexdigest()


def _frame(digest: hashlib._Hash, value: bytes) -> None:
    digest.update(len(value).to_bytes(8, "big"))
    digest.update(value)


def _validate_relative_path(path: PurePosixPath) -> None:
    if path.is_absolute() or not path.parts or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError("invalid active source path")


def _validate_receipt_shape(value: object, *, depth: int = 0, count: int = 0) -> int:
    if depth > _MAX_RECEIPT_DEPTH:
        raise ValueError("receipt is too deeply nested")
    count += 1
    if count > _MAX_RECEIPT_VALUES:
        raise ValueError("receipt has too many values")
    if isinstance(value, str):
        if len(value) > _MAX_RECEIPT_STRING:
            raise ValueError("receipt string is too long")
        return count
    if value is None or isinstance(value, bool | int | float):
        return count
    if isinstance(value, list):
        for item in value:
            count = _validate_receipt_shape(item, depth=depth + 1, count=count)
        return count
    if isinstance(value, dict):
        for key, item in value.items():
            if len(key) > _MAX_RECEIPT_STRING:
                raise ValueError("receipt key is too long")
            count = _validate_receipt_shape(item, depth=depth + 1, count=count)
        return count
    raise ValueError("unsupported receipt value")


def _write_receipt_file(destination: Path, content: bytes) -> None:
    try:
        target = _capture_output_target(destination)
        temporary = target.destination.with_name(
            f".{target.destination.name}.ubitofu-{uuid.uuid4().hex}.tmp"
        )
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(temporary, flags, 0o600)
        try:
            _write_all(fd, content)
            os.fchmod(fd, 0o600)
            os.fsync(fd)
        finally:
            os.close(fd)
        _recheck_output_target(target)
        os.replace(temporary, target.destination)
        _fsync_directory(target.destination.parent)
    except UbitofuError:
        raise
    except OSError as exc:
        raise UbitofuError("receipt output write failed") from exc
    finally:
        try:
            if "temporary" in locals() and os.path.lexists(temporary):
                os.unlink(temporary)
        except OSError:
            pass


def _capture_output_target(destination: Path) -> _OutputTarget:
    absolute = destination if destination.is_absolute() else Path.cwd() / destination
    owner_uid = _worktree_owner()
    parents = _capture_output_parents(absolute.parent)
    try:
        entry = os.lstat(absolute)
    except FileNotFoundError:
        return _OutputTarget(absolute, owner_uid, None, parents)
    facts = _file_facts(entry)
    _validate_output_file(facts, owner_uid)
    return _OutputTarget(absolute, owner_uid, facts, parents)


def _capture_output_parents(parent: Path) -> tuple[tuple[Path, _DirectoryFacts], ...]:
    parents: list[tuple[Path, _DirectoryFacts]] = []
    current = parent
    while True:
        try:
            entry = os.lstat(current)
        except OSError as exc:
            raise UbitofuError("unsafe receipt output") from exc
        facts = _directory_facts(entry)
        if stat.S_ISLNK(facts.mode) or not stat.S_ISDIR(facts.mode):
            raise UbitofuError("unsafe receipt output")
        parents.append((current, facts))
        if current.parent == current:
            return tuple(parents)
        current = current.parent


def _recheck_output_target(target: _OutputTarget) -> None:
    for path, expected in target.parents:
        try:
            actual = _directory_facts(os.lstat(path))
        except OSError as exc:
            raise UbitofuError("unsafe receipt output") from exc
        if actual != expected or stat.S_ISLNK(actual.mode) or not stat.S_ISDIR(actual.mode):
            raise UbitofuError("unsafe receipt output")
    try:
        actual_file = _file_facts(os.lstat(target.destination))
    except FileNotFoundError:
        actual_file = None
    if actual_file != target.destination_facts:
        raise UbitofuError("unsafe receipt output")
    if actual_file is not None:
        _validate_output_file(actual_file, target.owner_uid)


def _file_facts(entry: os.stat_result) -> _FileFacts:
    return _FileFacts(
        entry.st_dev,
        entry.st_ino,
        entry.st_mode,
        entry.st_uid,
        entry.st_size,
        entry.st_mtime_ns,
    )


def _directory_facts(entry: os.stat_result) -> _DirectoryFacts:
    return _DirectoryFacts(entry.st_dev, entry.st_ino, entry.st_mode, entry.st_uid)


def _validate_output_file(facts: _FileFacts, owner_uid: int) -> None:
    if stat.S_ISLNK(facts.mode) or not stat.S_ISREG(facts.mode) or facts.uid != owner_uid:
        raise UbitofuError("unsafe receipt output")


def _worktree_owner() -> int:
    worktree = Path.cwd().lstat()
    if stat.S_ISLNK(worktree.st_mode) or not stat.S_ISDIR(worktree.st_mode):
        raise UbitofuError("unsafe receipt output")
    return worktree.st_uid


def _write_all(fd: int, content: bytes) -> None:
    view = memoryview(content)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise OSError("short receipt write")
        view = view[written:]


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    fd = os.open(path, flags)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
