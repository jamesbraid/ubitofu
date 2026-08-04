# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 James Braid
"""Stable command outcomes, receipts, and intentionally small output plumbing."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import IO, Literal, cast

from .errors import UbitofuError
from .values import FrozenObject, FrozenValue, freeze_value

_SCHEMA = "dev.ubitofu.receipt"
_VERSION = 1
_MAX_RECEIPT_BYTES = 128 * 1024
_MAX_RECEIPT_DEPTH = 16
_MAX_RECEIPT_VALUES = 256
_MAX_RECEIPT_STRING = 240
_COMMAND_SUMMARIES: dict[str, str] = {
    "reconcile": "reconciliation complete",
}
_ITEM_VOCABULARY: dict[str, tuple[Literal["info", "warning", "blocking"], str]] = {
    "advisory": ("warning", "operator attention advised"),
    "captured_change": ("info", "captured controller changes"),
    "reconciliation_blocked": ("blocking", "reconciliation blocked"),
}
_ADDRESS_CHARACTERS = frozenset(
    'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.[]"-'
)
_INPUT_DIGEST_NAMES = frozenset({"active_source", "controller"})


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
        _validate_address(self.address)


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
        if _COMMAND_SUMMARIES.get(self.command) != self.summary:
            raise ValueError("unsupported public outcome summary")
        if not all(isinstance(item, OutcomeItem) for item in self.items):
            raise ValueError("outcome items must be typed")
        object.__setattr__(self, "items", tuple(sorted(self.items, key=_item_sort_key)))
        object.__setattr__(self, "input_digests", _normalise_digests(self.input_digests))
        object.__setattr__(self, "payload", _validate_payload(self.command, self.payload))


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
    return "\n".join(lines) + "\n"


def render_json(outcome: CommandOutcome) -> bytes:
    """Return canonical version-one receipt bytes."""
    return _canonical_json(_receipt_document(ReceiptEnvelope(outcome)))


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
        summary = value.get("summary", _COMMAND_SUMMARIES.get(command))
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


def _normalise_digests(values: tuple[tuple[str, str], ...]) -> tuple[tuple[str, str], ...]:
    normalised: list[tuple[str, str]] = []
    for name, digest in values:
        if not isinstance(name, str) or not isinstance(digest, str):
            raise ValueError("invalid input digest")
        if name not in _INPUT_DIGEST_NAMES:
            raise ValueError("invalid input digest")
        if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
            raise ValueError("invalid input digest")
        normalised.append((name, digest))
    sorted_values = tuple(sorted(normalised))
    if len({name for name, _ in sorted_values}) != len(sorted_values):
        raise ValueError("duplicate input digest")
    return sorted_values


def _item_sort_key(item: OutcomeItem) -> tuple[str, str, str, str]:
    return item.reason_code, item.address or "", item.severity, item.message


def _validate_address(value: str | None) -> None:
    if value is None:
        return
    if (
        not isinstance(value, str)
        or len(value) > 120
        or not value.isascii()
        or value.count(".") < 1
        or any(character not in _ADDRESS_CHARACTERS for character in value)
    ):
        raise ValueError("invalid public outcome address")


def _validate_payload(command: str, payload: FrozenValue | None) -> FrozenValue | None:
    if payload is None:
        return None
    if command != "reconcile" or not isinstance(payload, FrozenObject):
        raise ValueError("unsupported public outcome payload")
    values = dict(payload.items)
    if set(values) != {"changed_paths"} or len(values) != 1:
        raise ValueError("unsupported public outcome payload")
    paths = values["changed_paths"]
    if not isinstance(paths, tuple):
        raise ValueError("unsupported public outcome payload")
    for path in paths:
        if not isinstance(path, str):
            raise ValueError("unsupported public outcome payload")
        relative = PurePosixPath(path)
        _validate_relative_path(relative)
        if relative.as_posix() != path or not path.endswith((".tf", ".tofu", ".tf.json")):
            raise ValueError("unsupported public outcome payload")
    return freeze_value({"changed_paths": list(paths)})


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
