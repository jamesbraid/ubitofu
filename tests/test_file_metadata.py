# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 James Braid
"""Tests for fail-closed metadata inspection."""

from __future__ import annotations

import array
import os
import stat
import sys
from dataclasses import replace
from pathlib import PurePosixPath

import pytest

import ubitofu.file_metadata as file_metadata
from ubitofu.errors import UbitofuError
from ubitofu.file_metadata import (
    MetadataInspection,
    inspect_file_metadata,
    require_supported_metadata,
)


def test_inspection_captures_regular_file_identity(tmp_path) -> None:
    """Catches an inspection that omits ownership, mode, or content identity."""
    path = tmp_path / "main.tf"
    path.write_bytes(b"terraform {}\n")
    os.chmod(path, 0o640)

    inspection = inspect_file_metadata(path, relative_path=PurePosixPath("main.tf"))
    actual = path.lstat()

    assert inspection.identity.relative_path == PurePosixPath("main.tf")
    assert inspection.identity.device == actual.st_dev
    assert inspection.identity.inode == actual.st_ino
    assert inspection.identity.mode == actual.st_mode
    assert inspection.identity.uid == actual.st_uid
    assert inspection.identity.gid == actual.st_gid
    assert inspection.identity.size == len(b"terraform {}\n")
    assert len(inspection.identity.sha256) == 64
    assert inspection.xattr_names == ()
    assert isinstance(inspection.file_flags, int)


@pytest.mark.parametrize(
    ("field", "value"),
    (("acl_present", True), ("xattr_names", ("user.synthetic",)), ("file_flags", 1)),
)
def test_supported_metadata_rejects_extra_metadata(tmp_path, field, value) -> None:
    """Catches silent loss of ACLs, xattrs, or file flags during replacement."""
    path = tmp_path / "main.tf"
    path.write_text("terraform {}\n")
    inspection = inspect_file_metadata(path, relative_path=PurePosixPath("main.tf"))
    unsupported = replace(inspection, **{field: value})

    with pytest.raises(UbitofuError):
        require_supported_metadata(unsupported, worktree_uid=os.getuid())


def test_supported_metadata_rejects_owner_or_non_regular_file(tmp_path) -> None:
    """Catches replacing a file not owned by the administrator worktree owner."""
    path = tmp_path / "main.tf"
    path.write_text("terraform {}\n")
    inspection = inspect_file_metadata(path, relative_path=PurePosixPath("main.tf"))

    with pytest.raises(UbitofuError):
        require_supported_metadata(inspection, worktree_uid=inspection.identity.uid + 1)

    non_regular = replace(
        inspection,
        identity=replace(inspection.identity, mode=stat.S_IFDIR | 0o755),
    )
    with pytest.raises(UbitofuError):
        require_supported_metadata(non_regular, worktree_uid=inspection.identity.uid)


def test_inspection_fails_closed_when_xattrs_cannot_be_inspected(
    tmp_path, monkeypatch
) -> None:
    """Catches treating an unavailable metadata mechanism as proof of absence."""
    path = tmp_path / "main.tf"
    path.write_text("terraform {}\n")

    def unavailable(*args, **kwargs):
        raise OSError("synthetic metadata adapter failure")

    monkeypatch.setattr(file_metadata, "_list_xattrs", unavailable)
    with pytest.raises(UbitofuError):
        inspect_file_metadata(path, relative_path=PurePosixPath("main.tf"))


def test_acl_xattr_is_classified_as_acl_not_an_ordinary_xattr(
    tmp_path, monkeypatch
) -> None:
    """Catches accepting a POSIX ACL because it was only classified as an xattr."""
    path = tmp_path / "main.tf"
    path.write_text("terraform {}\n")
    monkeypatch.setattr(
        file_metadata,
        "_list_xattrs",
        lambda *args, **kwargs: ("system.posix_acl_access", "user.synthetic"),
    )

    inspection = inspect_file_metadata(path, relative_path=PurePosixPath("main.tf"))

    assert inspection.acl_present is True
    assert inspection.xattr_names == ("user.synthetic",)


def test_only_exact_macos_provenance_xattr_is_os_managed(
    tmp_path, monkeypatch
) -> None:
    """Catches broad prefix allowlisting of preservation-relevant Apple metadata."""
    path = tmp_path / "main.tf"
    path.write_text("terraform {}\n")
    monkeypatch.setattr(
        file_metadata,
        "_list_xattrs",
        lambda *args, **kwargs: ("com.apple.provenance",),
    )
    monkeypatch.setattr(file_metadata.sys, "platform", "darwin")
    monkeypatch.setattr(file_metadata, "_acl_present", lambda *args: False)
    monkeypatch.setattr(file_metadata, "_file_flags", lambda *args: 0)

    inspection = inspect_file_metadata(path, relative_path=PurePosixPath("main.tf"))

    assert inspection.xattr_names == ()
    assert require_supported_metadata(inspection, worktree_uid=os.getuid())


def test_macos_provenance_does_not_hide_a_second_xattr(
    tmp_path, monkeypatch
) -> None:
    """Catches accepting arbitrary xattrs whenever OS provenance is also present."""
    path = tmp_path / "main.tf"
    path.write_text("terraform {}\n")
    monkeypatch.setattr(
        file_metadata,
        "_list_xattrs",
        lambda *args, **kwargs: ("com.apple.provenance", "com.apple.synthetic"),
    )
    monkeypatch.setattr(file_metadata.sys, "platform", "darwin")
    monkeypatch.setattr(file_metadata, "_acl_present", lambda *args: False)
    monkeypatch.setattr(file_metadata, "_file_flags", lambda *args: 0)

    inspection = inspect_file_metadata(path, relative_path=PurePosixPath("main.tf"))

    assert inspection.xattr_names == ("com.apple.synthetic",)
    with pytest.raises(UbitofuError):
        require_supported_metadata(inspection, worktree_uid=os.getuid())


def test_linux_does_not_allow_the_macos_provenance_xattr(
    tmp_path, monkeypatch
) -> None:
    """Catches applying the Darwin-only provenance exception on Linux."""
    path = tmp_path / "main.tf"
    path.write_text("terraform {}\n")
    monkeypatch.setattr(file_metadata.sys, "platform", "linux")
    monkeypatch.setattr(
        file_metadata,
        "_list_xattrs",
        lambda *args, **kwargs: ("com.apple.provenance",),
    )
    monkeypatch.setattr(file_metadata, "_linux_file_flags", lambda *args: 0)

    inspection = inspect_file_metadata(path, relative_path=PurePosixPath("main.tf"))

    assert inspection.xattr_names == ("com.apple.provenance",)
    with pytest.raises(UbitofuError):
        require_supported_metadata(inspection, worktree_uid=os.getuid())


def test_linux_file_flags_are_read_through_the_ioctl_adapter(
    tmp_path, monkeypatch
) -> None:
    """Catches relying on absent Linux stat_result.st_flags and failing open."""
    path = tmp_path / "main.tf"
    path.write_text("terraform {}\n")
    monkeypatch.setattr(file_metadata.sys, "platform", "linux")
    monkeypatch.setattr(file_metadata, "_list_xattrs", lambda *args: ())
    monkeypatch.setattr(file_metadata, "_linux_file_flags", lambda *args: 0x10)

    inspection = inspect_file_metadata(path, relative_path=PurePosixPath("main.tf"))

    assert inspection.file_flags == 0x10
    with pytest.raises(UbitofuError):
        require_supported_metadata(inspection, worktree_uid=os.getuid())


def test_linux_ioctl_adapter_uses_fs_ioc_getflags(tmp_path, monkeypatch) -> None:
    """Catches issuing a different ioctl or reading an unmodified result buffer."""
    opened: list[tuple[object, int]] = []
    closed: list[int] = []

    def fake_open(path, flags):
        opened.append((path, flags))
        return 41

    def fake_ioctl(fd, request, values, mutate):
        assert fd == 41
        assert request == 0x80086601
        assert isinstance(values, array.array)
        assert mutate is True
        values[0] = 0x20
        return 0

    monkeypatch.setattr(file_metadata.os, "open", fake_open)
    monkeypatch.setattr(file_metadata.os, "close", closed.append)
    monkeypatch.setattr(file_metadata.fcntl, "ioctl", fake_ioctl)

    assert file_metadata._linux_file_flags(tmp_path / "main.tf") == 0x20
    assert opened and opened[0][0] == tmp_path / "main.tf"
    assert closed == [41]


def test_linux_file_flag_inspection_failure_blocks(tmp_path, monkeypatch) -> None:
    """Catches treating an unavailable FS_IOC_GETFLAGS mechanism as no flags."""
    path = tmp_path / "main.tf"
    path.write_text("terraform {}\n")
    monkeypatch.setattr(file_metadata.sys, "platform", "linux")
    monkeypatch.setattr(file_metadata, "_list_xattrs", lambda *args: ())

    def unavailable(*args):
        raise OSError("synthetic ioctl failure")

    monkeypatch.setattr(file_metadata, "_linux_file_flags", unavailable)

    with pytest.raises(UbitofuError):
        inspect_file_metadata(path, relative_path=PurePosixPath("main.tf"))


@pytest.mark.parametrize("flags", (0x10, 0x80000 | 0x10, 0x40000000))
def test_linux_rejects_sensitive_or_unknown_flags(tmp_path, monkeypatch, flags) -> None:
    """Catches broad allowlisting around the structural extent bit."""
    path = tmp_path / "main.tf"
    path.write_text("terraform {}\n")
    identity = inspect_file_metadata(
        path, relative_path=PurePosixPath("main.tf")
    ).identity
    monkeypatch.setattr(file_metadata.sys, "platform", "linux")
    inspection = MetadataInspection(
        identity=identity,
        acl_present=False,
        xattr_names=(),
        file_flags=flags,
    )

    with pytest.raises(UbitofuError):
        require_supported_metadata(inspection, worktree_uid=os.getuid())


def test_linux_accepts_only_the_structural_extent_flag(tmp_path, monkeypatch) -> None:
    """Catches rejecting an ordinary ext4 file solely for FS_EXTENT_FL."""
    path = tmp_path / "main.tf"
    path.write_text("terraform {}\n")
    identity = inspect_file_metadata(
        path, relative_path=PurePosixPath("main.tf")
    ).identity
    monkeypatch.setattr(file_metadata.sys, "platform", "linux")
    inspection = MetadataInspection(
        identity=identity,
        acl_present=False,
        xattr_names=(),
        file_flags=0x80000,
    )

    assert require_supported_metadata(inspection, worktree_uid=os.getuid())


@pytest.mark.skipif(sys.platform != "linux", reason="requires a real Linux filesystem")
def test_real_linux_tmp_file_metadata_is_accepted(tmp_path) -> None:
    """Catches a Linux adapter contract that rejects an ordinary real file."""
    path = tmp_path / "main.tf"
    path.write_text("terraform {}\n")

    inspection = inspect_file_metadata(path, relative_path=PurePosixPath("main.tf"))

    assert require_supported_metadata(inspection, worktree_uid=os.getuid())


def test_require_supported_metadata_returns_the_inspected_identity(tmp_path) -> None:
    """Catches callers reconstructing a weaker identity after validation."""
    path = tmp_path / "main.tf"
    path.write_text("terraform {}\n")
    inspection = MetadataInspection(
        identity=inspect_file_metadata(
            path, relative_path=PurePosixPath("main.tf")
        ).identity,
        acl_present=False,
        xattr_names=(),
        file_flags=0,
    )

    assert (
        require_supported_metadata(inspection, worktree_uid=inspection.identity.uid)
        is inspection.identity
    )
