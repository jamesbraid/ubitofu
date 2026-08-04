# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 James Braid
"""Private, lock-owned runtime artifacts for one workdir command."""

import fcntl
import os
import stat
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from .errors import UbitofuError

_MANIFEST = ".ubitofu-manifest"
_ARTIFACTS = ("tf.plan", "generated_stub.tf", _MANIFEST)


class RuntimeBusyError(UbitofuError):
    pass


@dataclass(frozen=True)
class RuntimeSession:
    workdir: Path
    private_root: Path
    run_root: Path
    plan_path: Path
    generated_path: Path


def _lstat_dir(path: Path) -> None:
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        path.mkdir(mode=0o700)
        mode = path.lstat().st_mode
    if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
        raise UbitofuError("unsafe runtime control path")
    os.chmod(path, 0o700)


def _inside(child: Path, parent: Path) -> bool:
    return child.resolve(strict=False).parent == parent.resolve(strict=True)


def _regular(path: Path) -> None:
    mode = path.lstat().st_mode
    if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
        raise UbitofuError("unsafe runtime artifact")


def _clean_child(child: Path, tmp_root: Path) -> None:
    if child.parent != tmp_root or not _inside(child, tmp_root):
        raise UbitofuError("runtime residue escapes temporary root")
    mode = child.lstat().st_mode
    if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
        raise UbitofuError("malformed runtime residue")
    manifest = child / _MANIFEST
    _regular(manifest)
    if manifest.read_text(encoding="ascii") != "tf.plan\ngenerated_stub.tf\n":
        raise UbitofuError("malformed runtime residue")
    for name in _ARTIFACTS:
        path = child / name
        _regular(path)
        path.unlink()
    child.rmdir()


def _recover_residue(tmp_root: Path) -> None:
    for child in tmp_root.iterdir():
        if len(child.name) != 32 or any(c not in "0123456789abcdef" for c in child.name):
            raise UbitofuError("malformed runtime residue")
        _clean_child(child, tmp_root)


@contextmanager
def runtime_session(workdir: Path, *, blocking: bool = True) -> Iterator[RuntimeSession]:
    """Hold a workdir lock while owning one small private artifact directory."""
    resolved_workdir = workdir.resolve()
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
    try:
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    except OSError as exc:
        raise UbitofuError("unsafe runtime control path") from exc
    os.chmod(lock_path, 0o600)
    try:
        flags = fcntl.LOCK_EX | (fcntl.LOCK_NB if not blocking else 0)
        try:
            fcntl.flock(fd, flags)
        except BlockingIOError as exc:
            raise RuntimeBusyError("workdir is busy") from exc
        _recover_residue(tmp_root)
        run_root = tmp_root / uuid.uuid4().hex
        run_root.mkdir(mode=0o700)
        plan_path = run_root / "tf.plan"
        generated_path = run_root / "generated_stub.tf"
        for path in (plan_path, generated_path):
            path.touch(mode=0o600)
            os.chmod(path, 0o600)
        manifest = run_root / _MANIFEST
        manifest.write_text("tf.plan\ngenerated_stub.tf\n", encoding="ascii")
        os.chmod(manifest, 0o600)
        session = RuntimeSession(
            resolved_workdir, private_root, run_root, plan_path, generated_path
        )
        try:
            yield session
        finally:
            _clean_child(run_root, tmp_root)
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
