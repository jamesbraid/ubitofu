# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 James Braid
"""Fail-closed metadata inspection for administrator-owned source files."""

from __future__ import annotations

import array
import ctypes
import fcntl
import hashlib
import os
import stat
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from .errors import UbitofuError
from .reconcile_model import FileIdentity

_ACL_XATTRS = frozenset(
    {
        "system.posix_acl_access",
        "system.posix_acl_default",
        "com.apple.system.Security",
    }
)
# Current macOS attaches this protected OS provenance record to newly created
# files and does not remove it on request. It is not operator metadata and is
# the only Darwin xattr excluded from the preservation contract.
_OS_MANAGED_XATTRS = frozenset({"com.apple.provenance"})


@dataclass(frozen=True)
class MetadataInspection:
    identity: FileIdentity
    acl_present: bool
    xattr_names: tuple[str, ...]
    file_flags: int


def inspect_file_metadata(
    path: Path,
    *,
    relative_path: PurePosixPath | None = None,
) -> MetadataInspection:
    """Inspect one path and fail unless all preservation-relevant facts are known."""
    try:
        before = path.lstat()
        names = tuple(sorted(_list_xattrs(path)))
        acl_present = _acl_present(path, names)
        file_flags = _file_flags(path, before)
        after = path.lstat()
    except (OSError, UnicodeError) as exc:
        raise UbitofuError("file metadata inspection failed") from exc
    if _stat_identity(before) != _stat_identity(after):
        raise UbitofuError("file changed during metadata inspection")
    sha256 = _sha256_regular(path, after)
    ordinary_xattrs = tuple(
        name
        for name in names
        if name not in _ACL_XATTRS
        and not (sys.platform == "darwin" and name in _OS_MANAGED_XATTRS)
    )
    return MetadataInspection(
        identity=FileIdentity(
            relative_path=relative_path or PurePosixPath(path.name),
            device=after.st_dev,
            inode=after.st_ino,
            mode=after.st_mode,
            uid=after.st_uid,
            gid=after.st_gid,
            size=after.st_size,
            mtime_ns=after.st_mtime_ns,
            sha256=sha256,
        ),
        acl_present=acl_present,
        xattr_names=ordinary_xattrs,
        file_flags=file_flags,
    )


def require_supported_metadata(
    inspection: MetadataInspection,
    *,
    worktree_uid: int,
) -> FileIdentity:
    """Return the complete identity only for metadata version 0.10 preserves."""
    identity = inspection.identity
    if not stat.S_ISREG(identity.mode):
        raise UbitofuError("transaction destination is not a regular file")
    if identity.uid != worktree_uid:
        raise UbitofuError("transaction destination has an unsupported owner")
    if inspection.acl_present:
        raise UbitofuError("transaction destination has an ACL")
    if inspection.xattr_names:
        raise UbitofuError("transaction destination has extended attributes")
    if inspection.file_flags:
        raise UbitofuError("transaction destination has file flags")
    return identity


def _sha256_regular(path: Path, expected: os.stat_result) -> str:
    if not stat.S_ISREG(expected.st_mode):
        return ""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
        try:
            opened = os.fstat(fd)
            if _stat_identity(opened) != _stat_identity(expected):
                raise UbitofuError("file changed during metadata inspection")
            digest = hashlib.sha256()
            while chunk := os.read(fd, 1024 * 1024):
                digest.update(chunk)
            if _stat_identity(os.fstat(fd)) != _stat_identity(expected):
                raise UbitofuError("file changed during metadata inspection")
            return digest.hexdigest()
        finally:
            os.close(fd)
    except OSError as exc:
        raise UbitofuError("file content inspection failed") from exc


def _stat_identity(value: os.stat_result) -> tuple[int, ...]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_uid,
        value.st_gid,
        value.st_size,
        value.st_mtime_ns,
    )


def _acl_present(path: Path, xattr_names: tuple[str, ...]) -> bool:
    if any(name in _ACL_XATTRS for name in xattr_names):
        return True
    if sys.platform != "darwin":
        return False
    try:
        result = subprocess.run(
            ["/bin/ls", "-lde", str(path)],
            capture_output=True,
            check=False,
            env={"LC_ALL": "C", "PATH": "/usr/bin:/bin"},
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise UbitofuError("ACL inspection failed") from exc
    first_line = result.stdout.splitlines()[0] if result.stdout else ""
    fields = first_line.split(maxsplit=1)
    if result.returncode != 0 or not fields or len(fields[0]) < 10:
        raise UbitofuError("ACL inspection failed")
    return fields[0].endswith("+")


def _file_flags(path: Path, inspected: os.stat_result) -> int:
    if sys.platform == "darwin":
        try:
            return int(inspected.st_flags)
        except AttributeError as exc:
            raise UbitofuError("file flag inspection is unavailable") from exc
    if sys.platform == "linux":
        try:
            return _linux_file_flags(path)
        except OSError as exc:
            raise UbitofuError("file flag inspection failed") from exc
    raise UbitofuError("file flag inspection is unavailable")


def _linux_file_flags(path: Path) -> int:
    """Return Linux inode flags through FS_IOC_GETFLAGS."""
    read_direction = 2
    request = (
        (read_direction << 30)
        | (ctypes.sizeof(ctypes.c_long) << 16)
        | (ord("f") << 8)
        | 1
    )
    values = array.array("l", [0])
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    try:
        fcntl.ioctl(fd, request, values, True)
    finally:
        os.close(fd)
    return int(values[0])


def _list_xattrs(path: Path) -> tuple[str, ...]:
    listxattr = getattr(os, "listxattr", None)
    if listxattr is not None:
        return tuple(listxattr(path, follow_symlinks=False))
    if sys.platform != "darwin":
        raise UbitofuError("extended attribute inspection is unavailable")
    try:
        result = subprocess.run(
            ["/usr/bin/xattr", str(path)],
            capture_output=True,
            check=False,
            env={"LC_ALL": "C", "PATH": "/usr/bin:/bin"},
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise UbitofuError("extended attribute inspection failed") from exc
    if result.returncode != 0:
        raise UbitofuError("extended attribute inspection failed")
    names = tuple(line for line in result.stdout.splitlines() if line)
    if any(any(ord(character) < 32 for character in name) for name in names):
        raise UbitofuError("invalid extended attribute name")
    return names
