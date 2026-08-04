# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 James Braid
"""Recoverable all-or-nothing commits for complete in-memory candidate sets."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import uuid
from dataclasses import dataclass, replace
from enum import Enum
from pathlib import Path, PurePosixPath
from typing import Literal, cast

from .errors import UbitofuError
from .file_metadata import inspect_file_metadata, require_supported_metadata
from .reconcile_model import FileIdentity
from .reconcile_renderer import ProposedFile

_PROTOCOL_VERSION = 1
_MAX_DOCUMENT_BYTES = 1024 * 1024
_MANIFEST_NAME = "manifest.json"
_JOURNAL_NAME = "journal.json"
_BACKUPS_NAME = "backups"
_CANDIDATES_NAME = "candidates"


class TransactionPhase(Enum):
    PREPARED = "prepared"
    COMMITTING = "committing"
    ROLLING_BACK = "rolling_back"
    COMMITTED = "committed"
    QUARANTINED = "quarantined"


@dataclass(frozen=True)
class TransactionEntry:
    relative_path: PurePosixPath
    operation: Literal["create", "replace", "delete"]
    original: FileIdentity | None
    original_sha256: str | None
    candidate_sha256: str | None
    backup_sha256: str | None
    destination_temp_name: str | None
    mode: int


@dataclass(frozen=True)
class RecoveryResult:
    transaction_id: str
    disposition: Literal["recovered_old", "verified_new", "quarantined"]
    affected_paths: tuple[PurePosixPath, ...]


@dataclass(frozen=True)
class _Journal:
    phase: TransactionPhase
    intents: tuple[PurePosixPath, ...]
    applied: tuple[PurePosixPath, ...]


@dataclass(frozen=True)
class PreparedTransaction:
    workdir: Path
    transaction_id: str
    transaction_root: Path
    entries: tuple[TransactionEntry, ...]
    worktree_uid: int
    worktree_gid: int
    no_op: bool = False

    def validate_sources(self) -> None:
        """Validate every private artifact and destination before the first write."""
        if self.no_op:
            return
        _validate_private_artifacts(self)
        for entry in self.entries:
            _require_expected_destination(self, entry)

    def commit(self) -> None:
        """Install all candidates, or restore every entry changed by this commit."""
        if self.no_op:
            return
        try:
            self.validate_sources()
        except BaseException as exc:
            try:
                # No intent exists yet. If the private material itself is
                # intact, a normal stale-source refusal is safe to clean up.
                _validate_private_artifacts(self)
                _cleanup_transaction(self)
            except BaseException:
                pass
            raise UbitofuError("transaction validation failed") from exc
        journal = _Journal(TransactionPhase.COMMITTING, (), ())
        _write_journal(self, journal)
        applied: list[TransactionEntry] = []
        try:
            for index, entry in enumerate(self.entries):
                _require_expected_destination(self, entry)
                temporary = _prepare_destination_temp(self, index, entry)
                journal = replace(
                    journal,
                    intents=journal.intents + (entry.relative_path,),
                )
                _write_journal(self, journal)
                _require_expected_destination(self, entry)
                # From this point the destination may have changed even if the
                # replacement's following directory fsync raises.
                applied.append(entry)
                _apply_entry(self, entry, temporary)
                journal = replace(
                    journal,
                    applied=journal.applied + (entry.relative_path,),
                )
                _write_journal(self, journal)
            journal = replace(journal, phase=TransactionPhase.COMMITTED)
            _write_journal(self, journal)
            _cleanup_transaction(self)
        except BaseException as exc:
            try:
                _write_journal(self, replace(journal, phase=TransactionPhase.ROLLING_BACK))
                _rollback_applied(self, tuple(applied))
                _cleanup_transaction(self)
            except BaseException as rollback_exc:
                _quarantine(self)
                raise UbitofuError("transaction rollback is ambiguous") from rollback_exc
            raise UbitofuError("transaction commit failed and was rolled back") from exc


def prepare_transaction(
    *,
    workdir: Path,
    files: tuple[ProposedFile, ...],
) -> PreparedTransaction:
    """Durably prepare complete backups and candidates without changing HCL."""
    resolved_workdir = workdir.resolve(strict=True)
    worktree = resolved_workdir.lstat()
    if not stat.S_ISDIR(worktree.st_mode):
        raise UbitofuError("transaction workdir is not a directory")
    if not files:
        return PreparedTransaction(
            resolved_workdir, "", resolved_workdir, (), worktree.st_uid, worktree.st_gid, True
        )
    sorted_files = tuple(sorted(files, key=lambda item: item.relative_path.as_posix()))
    paths = tuple(item.relative_path for item in sorted_files)
    if len(set(paths)) != len(paths):
        raise UbitofuError("transaction contains duplicate paths")
    for item in sorted_files:
        _validate_relative_path(item.relative_path)
        _validate_candidate_contract(item)
        _safe_destination(resolved_workdir, item.relative_path)
        _prevalidate_proposed(resolved_workdir, item, worktree_uid=worktree.st_uid)

    private_root = resolved_workdir / ".ubitofu"
    transactions_root = private_root / "transactions"
    _ensure_private_directory(private_root, owner_uid=worktree.st_uid)
    _fsync_directory(resolved_workdir)
    _ensure_private_directory(transactions_root, owner_uid=worktree.st_uid)
    _fsync_directory(private_root)
    residues = tuple(transactions_root.iterdir())
    if residues:
        raise UbitofuError("residual file transaction requires recovery")

    transaction_id = uuid.uuid4().hex
    transaction_root = transactions_root / transaction_id
    transaction_root.mkdir(mode=0o700)
    backups = transaction_root / _BACKUPS_NAME
    candidates = transaction_root / _CANDIDATES_NAME
    backups.mkdir(mode=0o700)
    candidates.mkdir(mode=0o700)

    entries: list[TransactionEntry] = []
    try:
        for index, proposed in enumerate(sorted_files):
            entry = _prepare_entry(
                resolved_workdir,
                transaction_id,
                transaction_root,
                index,
                proposed,
                worktree_uid=worktree.st_uid,
            )
            entries.append(entry)
        transaction = PreparedTransaction(
            resolved_workdir,
            transaction_id,
            transaction_root,
            tuple(entries),
            worktree.st_uid,
            worktree.st_gid,
        )
        _fsync_directory(backups)
        _fsync_directory(candidates)
        _fsync_directory(transaction_root)
        _write_manifest(transaction)
        _write_journal(transaction, _Journal(TransactionPhase.PREPARED, (), ()))
        _fsync_directory(transaction_root)
        _fsync_directory(transactions_root)
        transaction.validate_sources()
        return transaction
    except BaseException:
        # Once any artifact exists, leave the exact transaction root for startup
        # recovery rather than risk broad cleanup of partially prepared state.
        raise


def recover_transactions(workdir: Path) -> tuple[RecoveryResult, ...]:
    """Recover one residual transaction or preserve ambiguity as quarantine."""
    resolved_workdir = workdir.resolve(strict=True)
    worktree = resolved_workdir.lstat()
    private_root = resolved_workdir / ".ubitofu"
    try:
        private_stat = private_root.lstat()
    except FileNotFoundError:
        return ()
    if (
        stat.S_ISLNK(private_stat.st_mode)
        or not stat.S_ISDIR(private_stat.st_mode)
        or private_stat.st_uid != worktree.st_uid
        or stat.S_IMODE(private_stat.st_mode) != 0o700
    ):
        raise UbitofuError("unsafe transaction control path")
    transactions_root = private_root / "transactions"
    try:
        transactions_stat = transactions_root.lstat()
    except FileNotFoundError:
        return ()
    if (
        stat.S_ISLNK(transactions_stat.st_mode)
        or not stat.S_ISDIR(transactions_stat.st_mode)
        or transactions_stat.st_uid != worktree.st_uid
        or stat.S_IMODE(transactions_stat.st_mode) != 0o700
    ):
        raise UbitofuError("unsafe transaction control path")
    roots = tuple(sorted(transactions_root.iterdir(), key=lambda path: path.name))
    if not roots:
        transactions_root.rmdir()
        return ()
    if len(roots) != 1:
        raise UbitofuError("multiple residual transactions require operator attention")
    root = roots[0]
    if not _valid_transaction_id(root.name) or root.is_symlink() or not root.is_dir():
        raise UbitofuError("malformed transaction residue")
    transaction, journal = _load_transaction(resolved_workdir, root)
    if journal.phase is TransactionPhase.QUARANTINED:
        return (_recovery_result(transaction, "quarantined"),)
    try:
        _validate_private_artifacts(transaction)
        if not _destination_temporaries_are_safe(transaction):
            return (_quarantined_result(transaction),)
        states = tuple(_destination_state(transaction, entry) for entry in transaction.entries)
        if journal.phase is TransactionPhase.PREPARED:
            if any(state != "old" for state in states):
                return (_quarantined_result(transaction),)
            _cleanup_transaction(transaction)
            return (_recovery_result(transaction, "recovered_old"),)
        if journal.phase is TransactionPhase.COMMITTED:
            if any(state != "new" for state in states):
                return (_quarantined_result(transaction),)
            _cleanup_transaction(transaction)
            return (_recovery_result(transaction, "verified_new"),)
        if any(state == "ambiguous" for state in states):
            return (_quarantined_result(transaction),)
        if all(state == "new" for state in states):
            _cleanup_transaction(transaction)
            return (_recovery_result(transaction, "verified_new"),)
        _rollback_recovery(transaction, states)
        _cleanup_transaction(transaction)
        return (_recovery_result(transaction, "recovered_old"),)
    except UbitofuError:
        raise
    except (OSError, ValueError) as exc:
        raise UbitofuError("transaction recovery failed") from exc


def _prepare_entry(
    workdir: Path,
    transaction_id: str,
    transaction_root: Path,
    index: int,
    proposed: ProposedFile,
    *,
    worktree_uid: int,
) -> TransactionEntry:
    destination = _safe_destination(workdir, proposed.relative_path)
    if proposed.original is None:
        if _lexists(destination):
            raise UbitofuError("create destination already exists")
        operation: Literal["create", "replace", "delete"] = "create"
        original = None
        original_sha256 = None
        backup_sha256 = None
    else:
        inspection = inspect_file_metadata(
            destination, relative_path=proposed.relative_path
        )
        original = require_supported_metadata(inspection, worktree_uid=worktree_uid)
        if original != proposed.original:
            raise UbitofuError("transaction source identity is stale")
        operation = "delete" if proposed.candidate is None else "replace"
        original_sha256 = original.sha256
        backup_path = transaction_root / _BACKUPS_NAME / _artifact_name(index)
        backup_sha256 = _copy_exact(destination, backup_path, expected=original.sha256)
    candidate_sha256 = proposed.candidate_sha256
    if proposed.candidate is not None:
        candidate_path = transaction_root / _CANDIDATES_NAME / _artifact_name(index)
        _write_private_file(candidate_path, proposed.candidate)
        if _sha256_path(candidate_path) != candidate_sha256:
            raise UbitofuError("candidate digest mismatch")
    temp_name = (
        None
        if proposed.candidate is None
        else f".ubitofu-{transaction_id}-{index:06d}.tmp"
    )
    return TransactionEntry(
        relative_path=proposed.relative_path,
        operation=operation,
        original=original,
        original_sha256=original_sha256,
        candidate_sha256=candidate_sha256,
        backup_sha256=backup_sha256,
        destination_temp_name=temp_name,
        mode=proposed.mode,
    )


def _prevalidate_proposed(
    workdir: Path,
    proposed: ProposedFile,
    *,
    worktree_uid: int,
) -> None:
    destination = _safe_destination(workdir, proposed.relative_path)
    if proposed.original is None:
        if _lexists(destination):
            raise UbitofuError("create destination already exists")
        return
    inspection = inspect_file_metadata(
        destination, relative_path=proposed.relative_path
    )
    actual = require_supported_metadata(inspection, worktree_uid=worktree_uid)
    if actual != proposed.original:
        raise UbitofuError("transaction source identity is stale")


def _validate_candidate_contract(proposed: ProposedFile) -> None:
    digest = (
        None
        if proposed.candidate is None
        else hashlib.sha256(proposed.candidate).hexdigest()
    )
    if digest != proposed.candidate_sha256:
        raise UbitofuError("candidate digest mismatch")
    if proposed.original is None and proposed.candidate is None:
        raise UbitofuError("empty transaction entry")
    if proposed.original is not None and proposed.original.relative_path != proposed.relative_path:
        raise UbitofuError("transaction source path mismatch")
    if proposed.original is not None and proposed.mode != proposed.original.mode:
        raise UbitofuError("transaction candidate changes source mode")
    if not stat.S_ISREG(proposed.mode):
        raise UbitofuError("candidate mode is not regular")


def _require_expected_destination(
    transaction: PreparedTransaction, entry: TransactionEntry
) -> None:
    destination = _safe_destination(transaction.workdir, entry.relative_path)
    if entry.original is None:
        if _lexists(destination):
            raise UbitofuError("create destination became occupied")
        return
    inspection = inspect_file_metadata(destination, relative_path=entry.relative_path)
    actual = require_supported_metadata(
        inspection, worktree_uid=transaction.worktree_uid
    )
    if actual != entry.original:
        raise UbitofuError("transaction source changed")


def _validate_private_artifacts(transaction: PreparedTransaction) -> None:
    _validate_private_layout(transaction)
    for index, entry in enumerate(transaction.entries):
        backup = transaction.transaction_root / _BACKUPS_NAME / _artifact_name(index)
        candidate = transaction.transaction_root / _CANDIDATES_NAME / _artifact_name(index)
        if entry.backup_sha256 is None:
            if _lexists(backup):
                raise UbitofuError("unexpected transaction backup")
        elif _sha256_private(backup) != entry.backup_sha256:
            raise UbitofuError("transaction backup changed")
        if entry.candidate_sha256 is None:
            if _lexists(candidate):
                raise UbitofuError("unexpected transaction candidate")
        elif _sha256_private(candidate) != entry.candidate_sha256:
            raise UbitofuError("transaction candidate changed")


def _prepare_destination_temp(
    transaction: PreparedTransaction,
    index: int,
    entry: TransactionEntry,
) -> Path | None:
    if entry.destination_temp_name is None:
        return None
    destination = _safe_destination(transaction.workdir, entry.relative_path)
    temporary = destination.parent / entry.destination_temp_name
    if _lexists(temporary):
        raise UbitofuError("journaled destination temporary already exists")
    source = transaction.transaction_root / _CANDIDATES_NAME / _artifact_name(index)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(temporary, flags, 0o600)
        try:
            with source.open("rb") as candidate:
                while chunk := candidate.read(1024 * 1024):
                    _write_all(fd, chunk)
            mode = stat.S_IMODE(entry.mode)
            os.fchmod(fd, mode)
            owner = entry.original
            uid = transaction.worktree_uid if owner is None else owner.uid
            gid = transaction.worktree_gid if owner is None else owner.gid
            os.fchown(fd, uid, gid)
            os.fsync(fd)
        finally:
            os.close(fd)
        inspection = inspect_file_metadata(
            temporary, relative_path=entry.relative_path
        )
        identity = require_supported_metadata(
            inspection, worktree_uid=transaction.worktree_uid
        )
        if identity.sha256 != entry.candidate_sha256 or stat.S_IMODE(identity.mode) != mode:
            raise UbitofuError("destination temporary verification failed")
        expected_gid = transaction.worktree_gid if owner is None else owner.gid
        if identity.gid != expected_gid:
            raise UbitofuError("destination temporary ownership changed")
        _fsync_directory(destination.parent)
        return temporary
    except BaseException:
        # Keep a created temporary for exact-name recovery. Its name was already
        # committed to the manifest during prepare.
        raise


def _apply_entry(
    transaction: PreparedTransaction,
    entry: TransactionEntry,
    temporary: Path | None,
) -> None:
    destination = _safe_destination(transaction.workdir, entry.relative_path)
    if entry.operation == "delete":
        destination.unlink()
    else:
        if temporary is None:
            raise UbitofuError("replacement has no journaled temporary")
        os.replace(temporary, destination)
    _fsync_directory(destination.parent)


def _rollback_applied(
    transaction: PreparedTransaction,
    applied: tuple[TransactionEntry, ...],
) -> None:
    for entry in reversed(applied):
        state = _destination_state(transaction, entry)
        if state == "old":
            continue
        if state != "new":
            raise UbitofuError("rollback destination is ambiguous")
        _restore_old(transaction, entry)


def _rollback_recovery(
    transaction: PreparedTransaction,
    states: tuple[Literal["old", "new", "ambiguous"], ...],
) -> None:
    for entry, state in reversed(tuple(zip(transaction.entries, states, strict=True))):
        if state == "new":
            _restore_old(transaction, entry)


def _restore_old(transaction: PreparedTransaction, entry: TransactionEntry) -> None:
    destination = _safe_destination(transaction.workdir, entry.relative_path)
    if entry.original is None:
        if _lexists(destination):
            destination.unlink()
            _fsync_directory(destination.parent)
        return
    index = transaction.entries.index(entry)
    backup = transaction.transaction_root / _BACKUPS_NAME / _artifact_name(index)
    temporary = destination.parent / f".ubitofu-{transaction.transaction_id}-{index:06d}.rollback"
    if _lexists(temporary):
        raise UbitofuError("rollback temporary already exists")
    _copy_for_replacement(
        backup,
        temporary,
        mode=entry.original.mode,
        uid=entry.original.uid,
        gid=entry.original.gid,
    )
    os.replace(temporary, destination)
    _fsync_directory(destination.parent)


def _destination_state(
    transaction: PreparedTransaction,
    entry: TransactionEntry,
) -> Literal["old", "new", "ambiguous"]:
    destination = _safe_destination(transaction.workdir, entry.relative_path)
    if not _lexists(destination):
        if entry.operation == "create":
            return "old"
        if entry.operation == "delete":
            return "new"
        return "ambiguous"
    try:
        inspection = inspect_file_metadata(destination, relative_path=entry.relative_path)
        identity = require_supported_metadata(
            inspection, worktree_uid=transaction.worktree_uid
        )
    except UbitofuError:
        return "ambiguous"
    if entry.original is not None and _matches_original_tree(identity, entry.original):
        return "old"
    expected_uid = (
        transaction.worktree_uid if entry.original is None else entry.original.uid
    )
    expected_gid = (
        transaction.worktree_gid if entry.original is None else entry.original.gid
    )
    if (
        identity.sha256 == entry.candidate_sha256
        and stat.S_IMODE(identity.mode) == stat.S_IMODE(entry.mode)
        and identity.uid == expected_uid
        and identity.gid == expected_gid
    ):
        return "new"
    return "ambiguous"


def _matches_original_tree(actual: FileIdentity, original: FileIdentity) -> bool:
    return (
        actual.sha256 == original.sha256
        and stat.S_IMODE(actual.mode) == stat.S_IMODE(original.mode)
        and actual.uid == original.uid
        and actual.gid == original.gid
        and actual.size == original.size
    )


def _write_manifest(transaction: PreparedTransaction) -> None:
    document = {
        "version": _PROTOCOL_VERSION,
        "transaction_id": transaction.transaction_id,
        "entries": [_entry_to_json(entry) for entry in transaction.entries],
    }
    _write_json_atomic(transaction.transaction_root / _MANIFEST_NAME, document)


def _write_journal(transaction: PreparedTransaction, journal: _Journal) -> None:
    document = {
        "version": _PROTOCOL_VERSION,
        "transaction_id": transaction.transaction_id,
        "phase": journal.phase.value,
        "intents": [path.as_posix() for path in journal.intents],
        "applied": [path.as_posix() for path in journal.applied],
    }
    _write_json_atomic(transaction.transaction_root / _JOURNAL_NAME, document)


def _load_transaction(
    workdir: Path, root: Path
) -> tuple[PreparedTransaction, _Journal]:
    worktree = workdir.lstat()
    _recover_atomic_document_residue(root)
    manifest = _read_json(root / _MANIFEST_NAME)
    if set(manifest) != {"version", "transaction_id", "entries"}:
        raise UbitofuError("invalid transaction manifest fields")
    if manifest["version"] != _PROTOCOL_VERSION:
        raise UbitofuError("unsupported transaction manifest version")
    transaction_id = manifest["transaction_id"]
    if transaction_id != root.name or not isinstance(transaction_id, str):
        raise UbitofuError("transaction manifest identity mismatch")
    raw_entries = manifest["entries"]
    if not isinstance(raw_entries, list) or not raw_entries:
        raise UbitofuError("invalid transaction manifest entries")
    entries = tuple(_entry_from_json(value) for value in raw_entries)
    paths = tuple(entry.relative_path for entry in entries)
    if len(set(paths)) != len(paths) or paths != tuple(sorted(paths)):
        raise UbitofuError("duplicate or unsorted transaction entries")
    for index, entry in enumerate(entries):
        expected_temp = (
            None
            if entry.candidate_sha256 is None
            else f".ubitofu-{transaction_id}-{index:06d}.tmp"
        )
        if entry.destination_temp_name != expected_temp:
            raise UbitofuError("transaction temporary name does not match its entry")
    transaction = PreparedTransaction(
        workdir,
        transaction_id,
        root,
        entries,
        worktree.st_uid,
        worktree.st_gid,
    )
    journal_path = root / _JOURNAL_NAME
    if not _lexists(journal_path):
        journal = _Journal(TransactionPhase.PREPARED, (), ())
        _write_journal(transaction, journal)
        return transaction, journal
    raw_journal = _read_json(journal_path)
    if set(raw_journal) != {"version", "transaction_id", "phase", "intents", "applied"}:
        raise UbitofuError("invalid transaction journal fields")
    if (
        raw_journal["version"] != _PROTOCOL_VERSION
        or raw_journal["transaction_id"] != transaction_id
    ):
        raise UbitofuError("transaction journal identity mismatch")
    try:
        phase = TransactionPhase(raw_journal["phase"])
    except (TypeError, ValueError) as exc:
        raise UbitofuError("invalid transaction journal phase") from exc
    intents = _path_list(raw_journal["intents"], entries)
    applied = _path_list(raw_journal["applied"], entries)
    if len(applied) > len(intents) or intents[: len(applied)] != applied:
        raise UbitofuError("invalid transaction journal ordering")
    return transaction, _Journal(phase, intents, applied)


def _entry_to_json(entry: TransactionEntry) -> dict[str, object]:
    return {
        "relative_path": entry.relative_path.as_posix(),
        "operation": entry.operation,
        "original": None if entry.original is None else _identity_to_json(entry.original),
        "original_sha256": entry.original_sha256,
        "candidate_sha256": entry.candidate_sha256,
        "backup_sha256": entry.backup_sha256,
        "destination_temp_name": entry.destination_temp_name,
        "mode": entry.mode,
    }


def _entry_from_json(value: object) -> TransactionEntry:
    if not isinstance(value, dict) or set(value) != {
        "relative_path",
        "operation",
        "original",
        "original_sha256",
        "candidate_sha256",
        "backup_sha256",
        "destination_temp_name",
        "mode",
    }:
        raise UbitofuError("invalid transaction entry fields")
    relative = _parse_relative(cast(object, value["relative_path"]))
    operation = value["operation"]
    if operation not in {"create", "replace", "delete"}:
        raise UbitofuError("invalid transaction operation")
    original = _identity_from_json(value["original"], relative)
    original_sha256 = _optional_digest(value["original_sha256"])
    candidate_sha256 = _optional_digest(value["candidate_sha256"])
    backup_sha256 = _optional_digest(value["backup_sha256"])
    temp_name = value["destination_temp_name"]
    mode = value["mode"]
    if not isinstance(mode, int) or isinstance(mode, bool) or not stat.S_ISREG(mode):
        raise UbitofuError("invalid transaction entry mode")
    expected_temp_prefix = ".ubitofu-"
    if temp_name is not None and (
        not isinstance(temp_name, str)
        or "/" in temp_name
        or not temp_name.startswith(expected_temp_prefix)
        or not temp_name.endswith(".tmp")
    ):
        raise UbitofuError("invalid destination temporary name")
    if operation == "create":
        valid = (
            original is None
            and original_sha256 is None
            and backup_sha256 is None
            and candidate_sha256 is not None
            and temp_name is not None
        )
    elif operation == "replace":
        valid = (
            original is not None
            and original_sha256 == original.sha256
            and backup_sha256 == original.sha256
            and candidate_sha256 is not None
            and temp_name is not None
        )
    else:
        valid = (
            original is not None
            and original_sha256 == original.sha256
            and backup_sha256 == original.sha256
            and candidate_sha256 is None
            and temp_name is None
        )
    if not valid:
        raise UbitofuError("inconsistent transaction entry")
    if original is not None and mode != original.mode:
        raise UbitofuError("transaction entry does not preserve source mode")
    return TransactionEntry(
        relative,
        cast(Literal["create", "replace", "delete"], operation),
        original,
        original_sha256,
        candidate_sha256,
        backup_sha256,
        temp_name,
        mode,
    )


def _identity_to_json(identity: FileIdentity) -> dict[str, object]:
    return {
        "device": identity.device,
        "inode": identity.inode,
        "mode": identity.mode,
        "uid": identity.uid,
        "gid": identity.gid,
        "size": identity.size,
        "mtime_ns": identity.mtime_ns,
        "sha256": identity.sha256,
    }


def _identity_from_json(value: object, relative: PurePosixPath) -> FileIdentity | None:
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != {
        "device",
        "inode",
        "mode",
        "uid",
        "gid",
        "size",
        "mtime_ns",
        "sha256",
    }:
        raise UbitofuError("invalid transaction identity fields")
    identity_fields = ("device", "inode", "mode", "uid", "gid", "size", "mtime_ns")
    integers = tuple(value[name] for name in identity_fields)
    if any(not isinstance(item, int) or isinstance(item, bool) or item < 0 for item in integers):
        raise UbitofuError("invalid transaction identity")
    digest = _optional_digest(value["sha256"])
    if digest is None:
        raise UbitofuError("invalid transaction identity digest")
    return FileIdentity(relative, *cast(tuple[int, int, int, int, int, int, int], integers), digest)


def _validate_private_layout(transaction: PreparedTransaction) -> None:
    expected_root = {_MANIFEST_NAME, _JOURNAL_NAME, _BACKUPS_NAME, _CANDIDATES_NAME}
    try:
        root_stat = transaction.transaction_root.lstat()
        if (
            stat.S_ISLNK(root_stat.st_mode)
            or not stat.S_ISDIR(root_stat.st_mode)
            or stat.S_IMODE(root_stat.st_mode) != 0o700
            or root_stat.st_uid != transaction.worktree_uid
        ):
            raise UbitofuError("unsafe transaction private root")
        if {path.name for path in transaction.transaction_root.iterdir()} != expected_root:
            raise UbitofuError("unexpected transaction private artifact")
        for directory in (
            transaction.transaction_root / _BACKUPS_NAME,
            transaction.transaction_root / _CANDIDATES_NAME,
        ):
            directory_stat = directory.lstat()
            mode = directory_stat.st_mode
            if (
                stat.S_ISLNK(mode)
                or not stat.S_ISDIR(mode)
                or stat.S_IMODE(mode) != 0o700
                or directory_stat.st_uid != transaction.worktree_uid
            ):
                raise UbitofuError("unsafe transaction private directory")
        for document_name in (_MANIFEST_NAME, _JOURNAL_NAME):
            document_stat = (transaction.transaction_root / document_name).lstat()
            if (
                stat.S_ISLNK(document_stat.st_mode)
                or not stat.S_ISREG(document_stat.st_mode)
                or stat.S_IMODE(document_stat.st_mode) != 0o600
                or document_stat.st_uid != transaction.worktree_uid
            ):
                raise UbitofuError("unsafe transaction private document")
        expected_backups = {
            _artifact_name(index)
            for index, entry in enumerate(transaction.entries)
            if entry.backup_sha256 is not None
        }
        expected_candidates = {
            _artifact_name(index)
            for index, entry in enumerate(transaction.entries)
            if entry.candidate_sha256 is not None
        }
        if {
            path.name for path in (transaction.transaction_root / _BACKUPS_NAME).iterdir()
        } != expected_backups:
            raise UbitofuError("unexpected transaction backup artifact")
        if {
            path.name for path in (transaction.transaction_root / _CANDIDATES_NAME).iterdir()
        } != expected_candidates:
            raise UbitofuError("unexpected transaction candidate artifact")
        for directory, names in (
            (transaction.transaction_root / _BACKUPS_NAME, expected_backups),
            (transaction.transaction_root / _CANDIDATES_NAME, expected_candidates),
        ):
            for name in names:
                artifact_stat = (directory / name).lstat()
                if (
                    stat.S_ISLNK(artifact_stat.st_mode)
                    or not stat.S_ISREG(artifact_stat.st_mode)
                    or stat.S_IMODE(artifact_stat.st_mode) != 0o600
                    or artifact_stat.st_uid != transaction.worktree_uid
                ):
                    raise UbitofuError("unsafe transaction private file")
    except OSError as exc:
        raise UbitofuError("transaction private inspection failed") from exc


def _cleanup_transaction(transaction: PreparedTransaction) -> None:
    _validate_private_layout(transaction)
    for entry in transaction.entries:
        if entry.destination_temp_name is None:
            continue
        temporary = (
            _safe_destination(transaction.workdir, entry.relative_path).parent
            / entry.destination_temp_name
        )
        if _lexists(temporary):
            if _sha256_private(temporary) != entry.candidate_sha256:
                raise UbitofuError("journaled destination temporary is ambiguous")
            temporary.unlink()
            _fsync_directory(temporary.parent)
    for index, entry in enumerate(transaction.entries):
        if entry.backup_sha256 is not None:
            (transaction.transaction_root / _BACKUPS_NAME / _artifact_name(index)).unlink()
        if entry.candidate_sha256 is not None:
            (transaction.transaction_root / _CANDIDATES_NAME / _artifact_name(index)).unlink()
    (transaction.transaction_root / _BACKUPS_NAME).rmdir()
    (transaction.transaction_root / _CANDIDATES_NAME).rmdir()
    (transaction.transaction_root / _MANIFEST_NAME).unlink()
    (transaction.transaction_root / _JOURNAL_NAME).unlink()
    transaction.transaction_root.rmdir()
    transactions = transaction.transaction_root.parent
    _fsync_directory(transactions)
    transactions.rmdir()
    _fsync_directory(transactions.parent)


def _destination_temporaries_are_safe(transaction: PreparedTransaction) -> bool:
    for entry in transaction.entries:
        if entry.destination_temp_name is None:
            continue
        destination = _safe_destination(transaction.workdir, entry.relative_path)
        temporary = destination.parent / entry.destination_temp_name
        if not _lexists(temporary):
            continue
        try:
            inspection = inspect_file_metadata(
                temporary, relative_path=entry.relative_path
            )
            identity = require_supported_metadata(
                inspection, worktree_uid=transaction.worktree_uid
            )
        except UbitofuError:
            return False
        expected_uid = (
            transaction.worktree_uid if entry.original is None else entry.original.uid
        )
        expected_gid = (
            transaction.worktree_gid if entry.original is None else entry.original.gid
        )
        if (
            identity.sha256 != entry.candidate_sha256
            or stat.S_IMODE(identity.mode) != stat.S_IMODE(entry.mode)
            or identity.uid != expected_uid
            or identity.gid != expected_gid
        ):
            return False
    return True


def _quarantine(transaction: PreparedTransaction) -> None:
    try:
        _write_journal(transaction, _Journal(TransactionPhase.QUARANTINED, (), ()))
    except BaseException:
        pass


def _quarantined_result(transaction: PreparedTransaction) -> RecoveryResult:
    _quarantine(transaction)
    return _recovery_result(transaction, "quarantined")


def _recovery_result(
    transaction: PreparedTransaction,
    disposition: Literal["recovered_old", "verified_new", "quarantined"],
) -> RecoveryResult:
    return RecoveryResult(
        transaction.transaction_id,
        disposition,
        tuple(entry.relative_path for entry in transaction.entries),
    )


def _safe_destination(workdir: Path, relative: PurePosixPath) -> Path:
    _validate_relative_path(relative)
    current = workdir
    for part in relative.parts[:-1]:
        current = current / part
        try:
            mode = current.lstat().st_mode
        except FileNotFoundError as exc:
            raise UbitofuError("transaction destination parent is missing") from exc
        if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
            raise UbitofuError("unsafe transaction destination component")
    destination = current / relative.name
    if destination.parent.resolve(strict=True) != current.resolve(strict=True):
        raise UbitofuError("transaction destination escapes workdir")
    return destination


def _validate_relative_path(relative: PurePosixPath) -> None:
    if (
        relative.is_absolute()
        or relative == PurePosixPath(".")
        or not relative.parts
        or any(part in {"", ".", ".."} for part in relative.parts)
    ):
        raise UbitofuError("invalid transaction relative path")


def _parse_relative(value: object) -> PurePosixPath:
    if not isinstance(value, str):
        raise UbitofuError("invalid transaction relative path")
    relative = PurePosixPath(value)
    _validate_relative_path(relative)
    if relative.as_posix() != value:
        raise UbitofuError("non-canonical transaction relative path")
    return relative


def _path_list(value: object, entries: tuple[TransactionEntry, ...]) -> tuple[PurePosixPath, ...]:
    if not isinstance(value, list):
        raise UbitofuError("invalid transaction journal paths")
    paths = tuple(_parse_relative(item) for item in value)
    expected = tuple(entry.relative_path for entry in entries)
    if len(set(paths)) != len(paths) or paths != expected[: len(paths)]:
        raise UbitofuError("invalid transaction journal paths")
    return paths


def _ensure_private_directory(path: Path, *, owner_uid: int) -> None:
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        path.mkdir(mode=0o700)
        mode = path.lstat().st_mode
    actual = path.lstat()
    if (
        stat.S_ISLNK(mode)
        or not stat.S_ISDIR(mode)
        or actual.st_uid != owner_uid
    ):
        raise UbitofuError("unsafe transaction control path")
    os.chmod(path, 0o700)


def _write_private_file(path: Path, content: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags, 0o600)
        try:
            _write_all(fd, content)
            os.fsync(fd)
        finally:
            os.close(fd)
        os.chmod(path, 0o600)
    except OSError as exc:
        raise UbitofuError("transaction private write failed") from exc


def _write_json_atomic(path: Path, document: dict[str, object]) -> None:
    encoded = _canonical_json_bytes(document)
    if len(encoded) > _MAX_DOCUMENT_BYTES:
        raise UbitofuError("transaction document is too large")
    temporary = path.with_name(f".{path.name}.tmp")
    if _lexists(temporary):
        temporary.unlink()
    _write_private_file(temporary, encoded)
    os.replace(temporary, path)
    _fsync_directory(path.parent)


def _read_json(path: Path) -> dict[str, object]:
    try:
        mode = path.lstat().st_mode
        if (
            stat.S_ISLNK(mode)
            or not stat.S_ISREG(mode)
            or path.stat().st_size > _MAX_DOCUMENT_BYTES
        ):
            raise UbitofuError("unsafe transaction document")
        raw = path.read_bytes()
        if not raw or len(raw) > _MAX_DOCUMENT_BYTES:
            raise UbitofuError("invalid transaction document size")
        text = raw.decode("ascii")
        value = json.loads(
            text,
            object_pairs_hook=_object_without_duplicate_keys,
            parse_constant=_reject_json_constant,
        )
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise UbitofuError("invalid transaction document") from exc
    if not isinstance(value, dict):
        raise UbitofuError("transaction document is not an object")
    document = cast(dict[str, object], value)
    if raw != _canonical_json_bytes(document):
        raise UbitofuError("transaction document is not canonical")
    return document


def _canonical_json_bytes(document: dict[str, object]) -> bytes:
    return json.dumps(
        document, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("ascii") + b"\n"


def _object_without_duplicate_keys(
    pairs: list[tuple[str, object]],
) -> dict[str, object]:
    document: dict[str, object] = {}
    for key, value in pairs:
        if key in document:
            raise ValueError("duplicate JSON object key")
        document[key] = value
    return document


def _reject_json_constant(value: str) -> object:
    raise ValueError(f"unsupported JSON constant: {value}")


def _recover_atomic_document_residue(root: Path) -> None:
    manifest = root / _MANIFEST_NAME
    manifest_temporary = root / f".{_MANIFEST_NAME}.tmp"
    if _lexists(manifest_temporary):
        temporary_document = _read_json(manifest_temporary)
        if _lexists(manifest):
            if _read_json(manifest) != temporary_document:
                raise UbitofuError("ambiguous transaction manifest publication")
            manifest_temporary.unlink()
        else:
            os.replace(manifest_temporary, manifest)
        _fsync_directory(root)

    journal = root / _JOURNAL_NAME
    journal_temporary = root / f".{_JOURNAL_NAME}.tmp"
    if _lexists(journal_temporary):
        _read_json(journal_temporary)
        if _lexists(journal):
            _read_json(journal)
        journal_temporary.unlink()
        _fsync_directory(root)


def _copy_exact(source: Path, destination: Path, *, expected: str) -> str:
    _write_private_file(destination, source.read_bytes())
    digest = _sha256_path(destination)
    if digest != expected:
        raise UbitofuError("transaction backup digest mismatch")
    return digest


def _copy_for_replacement(
    source: Path,
    destination: Path,
    *,
    mode: int,
    uid: int,
    gid: int,
) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(destination, flags, 0o600)
    try:
        with source.open("rb") as file:
            while chunk := file.read(1024 * 1024):
                _write_all(fd, chunk)
        os.fchmod(fd, stat.S_IMODE(mode))
        os.fchown(fd, uid, gid)
        os.fsync(fd)
    finally:
        os.close(fd)
    _fsync_directory(destination.parent)


def _write_all(fd: int, content: bytes) -> None:
    view = memoryview(content)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise OSError("short transaction write")
        view = view[written:]


def _sha256_private(path: Path) -> str:
    try:
        mode = path.lstat().st_mode
        if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
            raise UbitofuError("unsafe transaction private artifact")
        return _sha256_path(path)
    except OSError as exc:
        raise UbitofuError("transaction private artifact is unavailable") from exc


def _sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        while chunk := file.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    fd = os.open(path, flags)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _artifact_name(index: int) -> str:
    return f"{index:06d}.bin"


def _optional_digest(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or len(value) != 64 or any(
        character not in "0123456789abcdef" for character in value
    ):
        raise UbitofuError("invalid transaction digest")
    return value


def _valid_transaction_id(value: str) -> bool:
    return len(value) == 32 and all(character in "0123456789abcdef" for character in value)


def _lexists(path: Path) -> bool:
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    return True
