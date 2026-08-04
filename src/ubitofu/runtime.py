# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 James Braid
"""Private, lock-owned runtime artifacts for one workdir command."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import stat
import uuid
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Literal

from .errors import UbitofuError

_MANIFEST = ".ubitofu-manifest"
_MANIFEST_NEXT = ".ubitofu-manifest.next"
_PLAN = "tf.plan"
_GENERATED = "generated_stub.tf"
_PRIVATE_IMPORTS = "generation-imports.tf"
GENERATION_SCAFFOLD_PATH = PurePosixPath("ubitofu-imports.tf")
_SCAFFOLD = GENERATION_SCAFFOLD_PATH.as_posix()
_SCHEMA = "dev.ubitofu.runtime"
# Manifest v2 is the first protocol that records the hard-linked generation
# scaffold's exact digest and inode metadata. Recovery rejects every other version.
_VERSION = 2
_MAX_MANIFEST = 16 * 1024


class RuntimeBusyError(UbitofuError):
    pass


class ReservedScaffoldPathError(UbitofuError):
    """The root-only generation scaffold name is occupied without owned residue."""


@dataclass(frozen=True)
class RuntimeSession:
    workdir: Path
    private_root: Path
    run_root: Path
    plan_path: Path
    generated_path: Path


@dataclass(frozen=True)
class _ScaffoldFacts:
    sha256: str
    device: int
    inode: int
    uid: int
    gid: int
    mode: int
    size: int


def _lstat_dir(path: Path) -> None:
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        path.mkdir(mode=0o700)
        mode = path.lstat().st_mode
    if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
        raise UbitofuError("unsafe runtime control path")
    os.chmod(path, 0o700)


def _validate_private_dir(path: Path, *, owner_uid: int) -> None:
    try:
        facts = path.lstat()
    except OSError as exc:
        raise UbitofuError("unsafe runtime control path") from exc
    if (
        stat.S_ISLNK(facts.st_mode)
        or not stat.S_ISDIR(facts.st_mode)
        or facts.st_uid != owner_uid
        or stat.S_IMODE(facts.st_mode) != 0o700
    ):
        raise UbitofuError("unsafe runtime control path")


def _validate_private_lock(path: Path, *, owner_uid: int) -> None:
    try:
        facts = path.lstat()
    except OSError as exc:
        raise UbitofuError("unsafe runtime control path") from exc
    if (
        stat.S_ISLNK(facts.st_mode)
        or not stat.S_ISREG(facts.st_mode)
        or facts.st_uid != owner_uid
        or stat.S_IMODE(facts.st_mode) != 0o600
    ):
        raise UbitofuError("unsafe runtime control path")


def _prepare_block_control(
    workdir: Path, *, owner_uid: int
) -> tuple[Path, Path, Path]:
    """Validate detect-only control state before creating any missing clean state."""
    private_root = workdir / ".ubitofu"
    tmp_root = private_root / "tmp"
    lock_path = private_root / "lock"
    if os.path.lexists(workdir / _SCAFFOLD) and not private_root.exists():
        raise UbitofuError("runtime residue requires recovery")
    try:
        private_root.lstat()
    except FileNotFoundError:
        private_root.mkdir(mode=0o700)
        tmp_root.mkdir(mode=0o700)
        fd = os.open(
            lock_path,
            os.O_CREAT | os.O_EXCL | os.O_RDWR | os.O_NOFOLLOW,
            0o600,
        )
        os.close(fd)
        return private_root, tmp_root, lock_path

    _validate_private_dir(private_root, owner_uid=owner_uid)
    _block_on_transaction_residue(workdir)
    try:
        tmp_root.lstat()
    except FileNotFoundError:
        tmp_missing = True
        if os.path.lexists(workdir / _SCAFFOLD):
            raise UbitofuError("runtime residue requires recovery") from None
    else:
        tmp_missing = False
        _validate_private_dir(tmp_root, owner_uid=owner_uid)
        _block_on_runtime_residue(tmp_root, workdir)
    try:
        lock_path.lstat()
    except FileNotFoundError:
        lock_missing = True
    else:
        lock_missing = False
        _validate_private_lock(lock_path, owner_uid=owner_uid)
    if tmp_missing:
        tmp_root.mkdir(mode=0o700)
    if lock_missing:
        fd = os.open(
            lock_path,
            os.O_CREAT | os.O_EXCL | os.O_RDWR | os.O_NOFOLLOW,
            0o600,
        )
        os.close(fd)
    return private_root, tmp_root, lock_path


def _inside(child: Path, parent: Path) -> bool:
    return child.resolve(strict=False).parent == parent.resolve(strict=True)


def _regular_private(path: Path, *, owner_uid: int) -> os.stat_result:
    facts = path.lstat()
    if (
        stat.S_ISLNK(facts.st_mode)
        or not stat.S_ISREG(facts.st_mode)
        or facts.st_uid != owner_uid
        or stat.S_IMODE(facts.st_mode) != 0o600
    ):
        raise UbitofuError("unsafe runtime artifact")
    return facts


def _scaffold_facts(path: Path, *, owner_uid: int) -> _ScaffoldFacts:
    facts = _regular_private(path, owner_uid=owner_uid)
    return _ScaffoldFacts(
        _sha256(path),
        facts.st_dev,
        facts.st_ino,
        facts.st_uid,
        facts.st_gid,
        facts.st_mode,
        facts.st_size,
    )


def _facts_document(facts: _ScaffoldFacts) -> dict[str, object]:
    return {
        "path": _SCAFFOLD,
        "private": _PRIVATE_IMPORTS,
        "sha256": facts.sha256,
        "device": facts.device,
        "inode": facts.inode,
        "uid": facts.uid,
        "gid": facts.gid,
        "mode": facts.mode,
        "size": facts.size,
    }


def _manifest_document(session_id: str, facts: _ScaffoldFacts | None) -> dict[str, object]:
    return {
        "schema": _SCHEMA,
        "version": _VERSION,
        "session_id": session_id,
        "optional_artifacts": [_GENERATED, _PLAN],
        "scaffold": None if facts is None else _facts_document(facts),
    }


def _canonical_json(document: Mapping[str, object]) -> bytes:
    return json.dumps(document, sort_keys=True, separators=(",", ":")).encode("ascii") + b"\n"


def _write_new_manifest(path: Path, document: dict[str, object]) -> None:
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    try:
        _write_all(fd, _canonical_json(document))
        os.fsync(fd)
    finally:
        os.close(fd)


def _replace_manifest(run_root: Path, document: dict[str, object]) -> None:
    current = run_root / _MANIFEST
    successor = run_root / _MANIFEST_NEXT
    _write_new_manifest(successor, document)
    os.replace(successor, current)
    _fsync_directory(run_root)


def _read_manifest(path: Path, *, session_id: str) -> _ScaffoldFacts | None:
    facts = path.lstat()
    if (
        stat.S_ISLNK(facts.st_mode)
        or not stat.S_ISREG(facts.st_mode)
        or stat.S_IMODE(facts.st_mode) != 0o600
        or facts.st_size > _MAX_MANIFEST
    ):
        raise UbitofuError("malformed runtime residue")
    try:
        document = json.loads(
            path.read_bytes(),
            object_pairs_hook=_object_without_duplicates,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeDecodeError, ValueError) as exc:
        raise UbitofuError("malformed runtime residue") from exc
    if not isinstance(document, dict) or set(document) != {
        "schema",
        "version",
        "session_id",
        "optional_artifacts",
        "scaffold",
    }:
        raise UbitofuError("malformed runtime residue")
    if (
        document["schema"] != _SCHEMA
        or document["version"] != _VERSION
        or document["session_id"] != session_id
        or document["optional_artifacts"] != [_GENERATED, _PLAN]
    ):
        raise UbitofuError("malformed runtime residue")
    scaffold = document["scaffold"]
    if scaffold is None:
        return None
    if not isinstance(scaffold, dict) or set(scaffold) != {
        "path",
        "private",
        "sha256",
        "device",
        "inode",
        "uid",
        "gid",
        "mode",
        "size",
    }:
        raise UbitofuError("malformed runtime residue")
    if scaffold["path"] != _SCAFFOLD or scaffold["private"] != _PRIVATE_IMPORTS:
        raise UbitofuError("malformed runtime residue")
    sha256 = scaffold["sha256"]
    numeric = tuple(scaffold[name] for name in ("device", "inode", "uid", "gid", "mode", "size"))
    if (
        not isinstance(sha256, str)
        or len(sha256) != 64
        or any(char not in "0123456789abcdef" for char in sha256)
        or not all(
            isinstance(value, int) and not isinstance(value, bool) and value >= 0
            for value in numeric
        )
    ):
        raise UbitofuError("malformed runtime residue")
    return _ScaffoldFacts(sha256, *numeric)


def _recover_manifest_successor(
    run_root: Path,
    *,
    session_id: str,
    owner_uid: int,
    workdir: Path,
) -> None:
    successor = run_root / _MANIFEST_NEXT
    if not os.path.lexists(successor):
        return
    if os.path.lexists(workdir / _SCAFFOLD):
        raise UbitofuError("ambiguous generation scaffold residue")
    current = _read_manifest(run_root / _MANIFEST, session_id=session_id)
    pending = _read_manifest(successor, session_id=session_id)
    if current is not None or pending is None:
        raise UbitofuError("malformed runtime residue")
    private = run_root / _PRIVATE_IMPORTS
    if os.path.lexists(private):
        if _scaffold_facts(private, owner_uid=owner_uid) != pending:
            raise UbitofuError("generation scaffold evidence mismatch")
        private.unlink()
    successor.unlink()
    _fsync_directory(run_root)


def _clean_child(child: Path, tmp_root: Path, workdir: Path, *, owner_uid: int) -> None:
    if child.parent != tmp_root or not _inside(child, tmp_root):
        raise UbitofuError("runtime residue escapes temporary root")
    mode = child.lstat().st_mode
    if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
        raise UbitofuError("malformed runtime residue")
    _recover_manifest_successor(
        child,
        session_id=child.name,
        owner_uid=owner_uid,
        workdir=workdir,
    )
    facts = _read_manifest(child / _MANIFEST, session_id=child.name)
    allowed = {_MANIFEST, _PLAN, _GENERATED}
    if facts is not None:
        allowed.add(_PRIVATE_IMPORTS)
    try:
        entries = frozenset(entry.name for entry in child.iterdir())
    except OSError as exc:
        raise UbitofuError("malformed runtime residue") from exc
    if not entries <= allowed or _MANIFEST not in entries:
        raise UbitofuError("malformed runtime residue")
    if facts is not None:
        private = child / _PRIVATE_IMPORTS
        published = workdir / _SCAFFOLD
        private_exists = os.path.lexists(private)
        if os.path.lexists(published):
            if not private_exists:
                raise UbitofuError("generation scaffold evidence is missing")
            if _scaffold_facts(private, owner_uid=owner_uid) != facts:
                raise UbitofuError("generation scaffold evidence mismatch")
            _require_published_scaffold(published, facts, owner_uid=owner_uid)
            published.unlink()
            _fsync_directory(workdir)
        if private_exists:
            if _scaffold_facts(private, owner_uid=owner_uid) != facts:
                raise UbitofuError("generation scaffold evidence mismatch")
            private.unlink()
    for name in (_PLAN, _GENERATED):
        path = child / name
        if os.path.lexists(path):
            _regular_private(path, owner_uid=owner_uid)
            path.unlink()
    (child / _MANIFEST).unlink()
    child.rmdir()
    _fsync_directory(tmp_root)


def _recover_residue(tmp_root: Path, workdir: Path, *, owner_uid: int) -> None:
    children = tuple(tmp_root.iterdir())
    if os.path.lexists(workdir / _SCAFFOLD) and not children:
        raise ReservedScaffoldPathError("reserved generation scaffold path is occupied")
    for child in children:
        if len(child.name) != 32 or any(c not in "0123456789abcdef" for c in child.name):
            raise UbitofuError("malformed runtime residue")
        _clean_child(child, tmp_root, workdir, owner_uid=owner_uid)
    if os.path.lexists(workdir / _SCAFFOLD):
        raise ReservedScaffoldPathError("reserved generation scaffold path is occupied")


def _block_on_runtime_residue(tmp_root: Path, workdir: Path) -> None:
    """Detect runtime residue without interpreting, deleting, or rewriting it."""
    try:
        has_children = next(tmp_root.iterdir(), None) is not None
    except OSError as exc:
        raise UbitofuError("runtime residue requires recovery") from exc
    if has_children or os.path.lexists(workdir / _SCAFFOLD):
        raise UbitofuError("runtime residue requires recovery")


def _block_on_transaction_residue(workdir: Path) -> None:
    """Refuse any pending transaction without running its recovery protocol."""
    transactions = workdir / ".ubitofu" / "transactions"
    try:
        facts = transactions.lstat()
    except FileNotFoundError:
        return
    if stat.S_ISLNK(facts.st_mode) or not stat.S_ISDIR(facts.st_mode):
        raise UbitofuError("transaction residue requires recovery")
    try:
        has_children = next(transactions.iterdir(), None) is not None
    except OSError as exc:
        raise UbitofuError("transaction residue requires recovery") from exc
    if has_children:
        raise UbitofuError("transaction residue requires recovery")


def _require_published_scaffold(
    path: Path, expected: _ScaffoldFacts, *, owner_uid: int
) -> None:
    actual = _scaffold_facts(path, owner_uid=owner_uid)
    if actual != expected:
        raise UbitofuError("generation scaffold evidence mismatch")


@contextmanager
def generation_import_scaffold(
    session: RuntimeSession, content: bytes
) -> Iterator[Path]:
    """Publish one exact generation-only import file for one plan call."""
    if not isinstance(content, bytes) or not content:
        raise ValueError("generation scaffold must contain complete bytes")
    published = session.workdir / _SCAFFOLD
    if os.path.lexists(published):
        raise ReservedScaffoldPathError("reserved generation scaffold path is occupied")
    private = session.run_root / _PRIVATE_IMPORTS
    fd = os.open(private, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    try:
        _write_all(fd, content)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.chmod(private, 0o600)
    owner_uid = session.workdir.stat().st_uid
    facts = _scaffold_facts(private, owner_uid=owner_uid)
    _replace_manifest(session.run_root, _manifest_document(session.run_root.name, facts))
    try:
        os.link(private, published, follow_symlinks=False)
    except OSError as exc:
        raise UbitofuError("generation scaffold publication failed") from exc
    _fsync_directory(session.workdir)
    try:
        _require_published_scaffold(published, facts, owner_uid=owner_uid)
        yield published
    finally:
        _require_published_scaffold(published, facts, owner_uid=owner_uid)
        published.unlink()
        _fsync_directory(session.workdir)


@contextmanager
def runtime_session(
    workdir: Path,
    *,
    blocking: bool = True,
    recovery: Literal["recover", "block"] = "recover",
) -> Iterator[RuntimeSession]:
    """Hold a workdir lock while owning one small private artifact directory."""
    resolved_workdir = workdir.resolve(strict=True)
    worktree = resolved_workdir.lstat()
    if not stat.S_ISDIR(worktree.st_mode):
        raise UbitofuError("runtime workdir is not a directory")
    if recovery == "block":
        private_root, tmp_root, lock_path = _prepare_block_control(
            resolved_workdir, owner_uid=worktree.st_uid
        )
        lock_flags = os.O_RDWR | os.O_NOFOLLOW
    elif recovery == "recover":
        private_root = resolved_workdir / ".ubitofu"
        _lstat_dir(private_root)
        tmp_root = private_root / "tmp"
        _lstat_dir(tmp_root)
        lock_path = private_root / "lock"
        try:
            lock_mode = lock_path.lstat().st_mode
        except FileNotFoundError:
            lock_mode = 0
        if lock_mode and (stat.S_ISLNK(lock_mode) or not stat.S_ISREG(lock_mode)):
            raise UbitofuError("unsafe runtime control path")
        lock_flags = os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW
    else:
        raise ValueError("unsupported runtime recovery policy")
    try:
        fd = os.open(lock_path, lock_flags, 0o600)
    except OSError as exc:
        raise UbitofuError("unsafe runtime control path") from exc
    if recovery == "recover":
        os.chmod(lock_path, 0o600)
    try:
        flags = fcntl.LOCK_EX | (fcntl.LOCK_NB if not blocking else 0)
        try:
            fcntl.flock(fd, flags)
        except BlockingIOError as exc:
            raise RuntimeBusyError("workdir is busy") from exc
        if recovery == "recover":
            from .file_transaction import recover_transactions

            recovered = recover_transactions(resolved_workdir)
            if any(item.disposition == "quarantined" for item in recovered):
                raise UbitofuError(
                    "quarantined file transaction requires operator attention"
                )
            _recover_residue(tmp_root, resolved_workdir, owner_uid=worktree.st_uid)
        elif recovery == "block":
            _validate_private_dir(private_root, owner_uid=worktree.st_uid)
            _validate_private_dir(tmp_root, owner_uid=worktree.st_uid)
            _validate_private_lock(lock_path, owner_uid=worktree.st_uid)
            _block_on_transaction_residue(resolved_workdir)
            _block_on_runtime_residue(tmp_root, resolved_workdir)
        run_root = tmp_root / uuid.uuid4().hex
        run_root.mkdir(mode=0o700)
        _write_new_manifest(run_root / _MANIFEST, _manifest_document(run_root.name, None))
        _fsync_directory(run_root)
        _fsync_directory(tmp_root)
        session = RuntimeSession(
            resolved_workdir,
            private_root,
            run_root,
            run_root / _PLAN,
            run_root / _GENERATED,
        )
        try:
            yield session
        finally:
            _clean_child(
                run_root,
                tmp_root,
                resolved_workdir,
                owner_uid=worktree.st_uid,
            )
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_all(fd: int, content: bytes) -> None:
    view = memoryview(content)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise OSError("short runtime artifact write")
        view = view[written:]


def _fsync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _object_without_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate manifest key")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> object:
    raise ValueError(f"invalid JSON constant {value}")
