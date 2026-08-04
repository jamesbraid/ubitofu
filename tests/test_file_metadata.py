# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 James Braid
"""Tests for fail-closed metadata inspection."""

from __future__ import annotations

import os
import stat
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
    assert inspection.file_flags == getattr(actual, "st_flags", 0)


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

    inspection = inspect_file_metadata(path, relative_path=PurePosixPath("main.tf"))

    assert inspection.xattr_names == ("com.apple.synthetic",)
    with pytest.raises(UbitofuError):
        require_supported_metadata(inspection, worktree_uid=os.getuid())


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
