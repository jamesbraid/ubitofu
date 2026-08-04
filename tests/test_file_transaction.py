# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 James Braid
"""Tests for atomic multi-file candidate commits."""

from __future__ import annotations

import hashlib
import os
from dataclasses import replace
from pathlib import Path, PurePosixPath

import pytest

import ubitofu.file_transaction as file_transaction
from ubitofu.errors import UbitofuError
from ubitofu.file_metadata import inspect_file_metadata
from ubitofu.file_transaction import prepare_transaction, recover_transactions
from ubitofu.reconcile_renderer import ProposedFile


def _existing(workdir: Path, name: str, candidate: bytes | None) -> ProposedFile:
    path = workdir / name
    identity = inspect_file_metadata(path, relative_path=PurePosixPath(name)).identity
    return ProposedFile(
        relative_path=PurePosixPath(name),
        original=identity,
        candidate=candidate,
        candidate_sha256=(
            None if candidate is None else hashlib.sha256(candidate).hexdigest()
        ),
        mode=identity.mode,
    )


def _created(name: str, candidate: bytes) -> ProposedFile:
    return ProposedFile(
        relative_path=PurePosixPath(name),
        original=None,
        candidate=candidate,
        candidate_sha256=hashlib.sha256(candidate).hexdigest(),
        mode=0o100640,
    )


def test_commit_applies_create_replace_and_delete_in_sorted_order(tmp_path) -> None:
    """Catches partial or input-order-dependent candidate commits."""
    (tmp_path / "replace.tf").write_bytes(b"old replace\n")
    (tmp_path / "delete.tf").write_bytes(b"old delete\n")
    files = (
        _existing(tmp_path, "replace.tf", b"new replace\n"),
        _created("create.tf", b"new create\n"),
        _existing(tmp_path, "delete.tf", None),
    )

    transaction = prepare_transaction(workdir=tmp_path, files=files)
    assert tuple(entry.relative_path for entry in transaction.entries) == (
        PurePosixPath("create.tf"),
        PurePosixPath("delete.tf"),
        PurePosixPath("replace.tf"),
    )
    transaction.commit()

    assert (tmp_path / "create.tf").read_bytes() == b"new create\n"
    assert (tmp_path / "create.tf").stat().st_mode & 0o777 == 0o640
    assert not (tmp_path / "delete.tf").exists()
    assert (tmp_path / "replace.tf").read_bytes() == b"new replace\n"
    assert not (tmp_path / ".ubitofu" / "transactions").exists()


def test_empty_transaction_does_not_create_private_state(tmp_path) -> None:
    """Catches a no-op preview leaving recoverable transaction residue."""
    transaction = prepare_transaction(workdir=tmp_path, files=())
    transaction.validate_sources()
    transaction.commit()
    assert not (tmp_path / ".ubitofu").exists()


@pytest.mark.parametrize(
    "relative",
    (
        PurePosixPath("."),
        PurePosixPath("..", "escape.tf"),
        PurePosixPath("/absolute.tf"),
        PurePosixPath("nested", "..", "escape.tf"),
    ),
)
def test_prepare_rejects_root_or_escaping_paths(tmp_path, relative) -> None:
    """Catches candidate paths escaping the administrator-selected worktree."""
    file = replace(_created("main.tf", b"candidate\n"), relative_path=relative)
    with pytest.raises(UbitofuError):
        prepare_transaction(workdir=tmp_path, files=(file,))
    assert not (tmp_path / ".ubitofu").exists()


def test_prepare_rejects_duplicates_symlink_components_and_non_regular_files(
    tmp_path,
) -> None:
    """Catches ambiguous ownership and traversal through mutable path objects."""
    duplicate = _created("main.tf", b"candidate\n")
    with pytest.raises(UbitofuError):
        prepare_transaction(workdir=tmp_path, files=(duplicate, duplicate))

    outside = tmp_path / "outside"
    outside.mkdir()
    (tmp_path / "linked").symlink_to(outside, target_is_directory=True)
    with pytest.raises(UbitofuError):
        prepare_transaction(
            workdir=tmp_path, files=(_created("linked/main.tf", b"candidate\n"),)
        )

    fifo = tmp_path / "pipe.tf"
    os.mkfifo(fifo)
    with pytest.raises(UbitofuError):
        prepare_transaction(
            workdir=tmp_path,
            files=(
                ProposedFile(
                    PurePosixPath("pipe.tf"),
                    None,
                    b"candidate\n",
                    hashlib.sha256(b"candidate\n").hexdigest(),
                    0o100644,
                ),
            ),
        )


def test_prepare_rejects_stale_or_atomically_replaced_source(tmp_path) -> None:
    """Catches validating only bytes while ignoring an atomic-editor identity change."""
    path = tmp_path / "main.tf"
    path.write_bytes(b"old\n")
    proposed = _existing(tmp_path, "main.tf", b"new\n")
    path.write_bytes(b"changed\n")
    with pytest.raises(UbitofuError):
        prepare_transaction(workdir=tmp_path, files=(proposed,))
    assert not (tmp_path / ".ubitofu").exists()

    path.write_bytes(b"old\n")
    proposed = _existing(tmp_path, "main.tf", b"new\n")
    replacement = tmp_path / "editor.tmp"
    replacement.write_bytes(b"old\n")
    os.replace(replacement, path)
    with pytest.raises(UbitofuError):
        prepare_transaction(workdir=tmp_path, files=(proposed,))
    assert not (tmp_path / ".ubitofu").exists()


def test_prepare_rejects_a_mode_that_disagrees_with_the_original(tmp_path) -> None:
    """Catches a malformed ProposedFile changing permissions during replacement."""
    path = tmp_path / "main.tf"
    path.write_bytes(b"old\n")
    proposed = _existing(tmp_path, "main.tf", b"new\n")
    malformed = replace(proposed, mode=0o100600)

    with pytest.raises(UbitofuError):
        prepare_transaction(workdir=tmp_path, files=(malformed,))

    assert path.read_bytes() == b"old\n"
    assert path.stat().st_mode & 0o777 == 0o644
    assert not (tmp_path / ".ubitofu").exists()


def test_commit_rechecks_mode_and_owner_facts_after_prepare(tmp_path) -> None:
    """Catches a commit that only rehashes bytes after initial metadata inspection."""
    path = tmp_path / "main.tf"
    path.write_bytes(b"old\n")
    transaction = prepare_transaction(
        workdir=tmp_path, files=(_existing(tmp_path, "main.tf", b"new\n"),)
    )
    os.chmod(path, 0o600)

    with pytest.raises(UbitofuError):
        transaction.commit()
    assert path.read_bytes() == b"old\n"
    assert not (tmp_path / ".ubitofu" / "transactions").exists()


def test_private_candidate_tampering_blocks_commit_and_preserves_source(tmp_path) -> None:
    """Catches trusting prepared candidate bytes after their digest was journaled."""
    path = tmp_path / "main.tf"
    path.write_bytes(b"old\n")
    transaction = prepare_transaction(
        workdir=tmp_path, files=(_existing(tmp_path, "main.tf", b"new\n"),)
    )
    candidates = list((transaction.transaction_root / "candidates").iterdir())
    assert len(candidates) == 1
    candidates[0].write_bytes(b"tampered\n")

    with pytest.raises(UbitofuError):
        transaction.commit()
    assert path.read_bytes() == b"old\n"


def test_later_external_edit_rolls_back_already_applied_entry(
    tmp_path, monkeypatch
) -> None:
    """Catches missing per-entry rechecks after the initial full validation."""
    first = tmp_path / "a.tf"
    later = tmp_path / "b.tf"
    first.write_bytes(b"old a\n")
    later.write_bytes(b"old b\n")
    transaction = prepare_transaction(
        workdir=tmp_path,
        files=(
            _existing(tmp_path, "a.tf", b"new a\n"),
            _existing(tmp_path, "b.tf", b"new b\n"),
        ),
    )
    real_replace = os.replace
    edited = False

    def replace_and_edit(source, destination):
        nonlocal edited
        real_replace(source, destination)
        if Path(destination) == first and not edited:
            edited = True
            later.write_bytes(b"external b\n")

    monkeypatch.setattr(os, "replace", replace_and_edit)
    with pytest.raises(UbitofuError):
        transaction.commit()

    assert first.read_bytes() == b"old a\n"
    assert later.read_bytes() == b"external b\n"


def test_replace_failure_rolls_back_without_partial_tree(tmp_path, monkeypatch) -> None:
    """Catches an injected replacement failure leaving earlier candidates installed."""
    paths = [tmp_path / "a.tf", tmp_path / "b.tf"]
    for index, path in enumerate(paths):
        path.write_bytes(f"old {index}\n".encode())
    transaction = prepare_transaction(
        workdir=tmp_path,
        files=tuple(
            _existing(tmp_path, path.name, f"new {index}\n".encode())
            for index, path in enumerate(paths)
        ),
    )
    real_replace = os.replace

    def fail_second(source, destination):
        if Path(destination) == paths[1]:
            raise OSError("synthetic replace failure")
        real_replace(source, destination)

    monkeypatch.setattr(os, "replace", fail_second)
    with pytest.raises(UbitofuError):
        transaction.commit()

    assert [path.read_bytes() for path in paths] == [b"old 0\n", b"old 1\n"]


def test_directory_fsync_failure_after_replace_rolls_back(tmp_path, monkeypatch) -> None:
    """Catches losing replacement knowledge when its directory fsync fails."""
    path = tmp_path / "main.tf"
    path.write_bytes(b"old\n")
    transaction = prepare_transaction(
        workdir=tmp_path, files=(_existing(tmp_path, "main.tf", b"new\n"),)
    )
    real_fsync_directory = file_transaction._fsync_directory
    destination_calls = 0

    def fail_second_destination_fsync(directory):
        nonlocal destination_calls
        if Path(directory) == tmp_path:
            destination_calls += 1
            if destination_calls == 2:
                raise OSError("synthetic directory fsync failure")
        real_fsync_directory(directory)

    monkeypatch.setattr(
        file_transaction, "_fsync_directory", fail_second_destination_fsync
    )
    with pytest.raises(UbitofuError):
        transaction.commit()

    assert path.read_bytes() == b"old\n"
    assert not (tmp_path / ".ubitofu" / "transactions").exists()


def test_committed_journal_fsync_failure_preserves_complete_new_tree(
    tmp_path, monkeypatch
) -> None:
    """Catches rolling back after COMMITTED was replaced but not directory-fsynced."""
    path = tmp_path / "main.tf"
    path.write_bytes(b"old\n")
    transaction = prepare_transaction(
        workdir=tmp_path, files=(_existing(tmp_path, "main.tf", b"new\n"),)
    )
    real_fsync_directory = file_transaction._fsync_directory
    failed = False

    def fail_committed_journal_fsync(directory):
        nonlocal failed
        journal = transaction.transaction_root / "journal.json"
        if (
            not failed
            and Path(directory) == transaction.transaction_root
            and journal.exists()
            and b'"phase":"committed"' in journal.read_bytes()
        ):
            failed = True
            raise OSError("synthetic committed journal fsync failure")
        real_fsync_directory(directory)

    monkeypatch.setattr(
        file_transaction, "_fsync_directory", fail_committed_journal_fsync
    )
    with pytest.raises(UbitofuError):
        transaction.commit()

    assert failed
    assert path.read_bytes() == b"new\n"
    monkeypatch.setattr(file_transaction, "_fsync_directory", real_fsync_directory)
    assert recover_transactions(tmp_path)[0].disposition == "verified_new"
    assert path.read_bytes() == b"new\n"
