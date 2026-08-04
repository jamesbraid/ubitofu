# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 James Braid
"""Tests for durable file-transaction startup recovery."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path, PurePosixPath

import pytest

from ubitofu.errors import UbitofuError
from ubitofu.file_metadata import inspect_file_metadata
from ubitofu.file_transaction import prepare_transaction, recover_transactions
from ubitofu.reconcile_renderer import ProposedFile
from ubitofu.runtime import runtime_session


def _proposed(workdir: Path, name: str, candidate: bytes) -> ProposedFile:
    identity = inspect_file_metadata(
        workdir / name, relative_path=PurePosixPath(name)
    ).identity
    return ProposedFile(
        PurePosixPath(name),
        identity,
        candidate,
        hashlib.sha256(candidate).hexdigest(),
        identity.mode,
    )


def _transaction_root(workdir: Path) -> Path:
    children = list((workdir / ".ubitofu" / "transactions").iterdir())
    assert len(children) == 1
    return children[0]


def _reverse_json_key_order(raw: bytes) -> bytes:
    document = json.loads(raw)
    reversed_document = dict(reversed(tuple(document.items())))
    return json.dumps(
        reversed_document,
        sort_keys=False,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("ascii") + b"\n"


@pytest.mark.parametrize(
    "mutation",
    (
        lambda document: {**document, "version": 99},
        lambda document: {**document, "transaction_id": "../escape"},
        lambda document: {**document, "phase": "unknown"},
        lambda document: {**document, "unknown_required": True},
        lambda document: {**document, "entries": document["entries"] * 2},
    ),
)
def test_recovery_rejects_malformed_manifest_without_deleting_it(
    tmp_path, mutation
) -> None:
    """Catches permissive recovery parsing or deletion of ambiguous evidence."""
    path = tmp_path / "main.tf"
    path.write_bytes(b"old\n")
    prepare_transaction(
        workdir=tmp_path, files=(_proposed(tmp_path, "main.tf", b"new\n"),)
    )
    root = _transaction_root(tmp_path)
    manifest = root / "manifest.json"
    document = json.loads(manifest.read_text())
    manifest.write_text(json.dumps(mutation(document)))

    with pytest.raises(UbitofuError):
        recover_transactions(tmp_path)
    assert root.exists()
    assert path.read_bytes() == b"old\n"


@pytest.mark.parametrize("payload", (b"{", b"x" * (1024 * 1024 + 1)))
def test_recovery_rejects_truncated_or_oversized_json(tmp_path, payload) -> None:
    """Catches unbounded or partial journal decoding during startup."""
    path = tmp_path / "main.tf"
    path.write_bytes(b"old\n")
    prepare_transaction(
        workdir=tmp_path, files=(_proposed(tmp_path, "main.tf", b"new\n"),)
    )
    root = _transaction_root(tmp_path)
    (root / "journal.json").write_bytes(payload)

    with pytest.raises(UbitofuError):
        recover_transactions(tmp_path)
    assert root.exists()


@pytest.mark.parametrize(
    "mutate",
    (
        lambda raw: raw.replace(b'{"entries":', b'{"entries":[],"entries":', 1),
        lambda raw: b" " + raw,
        _reverse_json_key_order,
        lambda raw: raw.replace(b"main.tf", b"main\\u002etf", 1),
        lambda raw: raw + b"{}\n",
    ),
    ids=(
        "duplicate-key",
        "whitespace",
        "key-order",
        "alternate-escape",
        "trailing-data",
    ),
)
def test_recovery_requires_exact_canonical_json_bytes(tmp_path, mutate) -> None:
    """Catches accepting an ambiguous encoding of durable recovery facts."""
    path = tmp_path / "main.tf"
    path.write_bytes(b"old\n")
    prepare_transaction(
        workdir=tmp_path, files=(_proposed(tmp_path, "main.tf", b"new\n"),)
    )
    root = _transaction_root(tmp_path)
    manifest = root / "manifest.json"
    manifest.write_bytes(mutate(manifest.read_bytes()))

    with pytest.raises(UbitofuError):
        recover_transactions(tmp_path)
    assert root.exists()
    assert path.read_bytes() == b"old\n"


def test_recovery_refuses_multiple_residual_transactions(tmp_path) -> None:
    """Catches guessing an unsafe recovery order for multiple residual commits."""
    path = tmp_path / "main.tf"
    path.write_bytes(b"old\n")
    first = prepare_transaction(
        workdir=tmp_path, files=(_proposed(tmp_path, "main.tf", b"new\n"),)
    )
    other = first.transaction_root.parent / ("f" * 32)
    other.mkdir(mode=0o700)

    with pytest.raises(UbitofuError):
        recover_transactions(tmp_path)
    assert first.transaction_root.exists()
    assert other.exists()


def test_recovery_refuses_a_symlinked_transaction_control_path(tmp_path) -> None:
    """Catches recovery following a private-root symlink outside the worktree."""
    outside = tmp_path / "outside"
    outside.mkdir()
    marker = outside / "marker"
    marker.write_bytes(b"keep\n")
    (tmp_path / ".ubitofu").symlink_to(outside, target_is_directory=True)

    with pytest.raises(UbitofuError):
        recover_transactions(tmp_path)
    assert marker.read_bytes() == b"keep\n"


@pytest.mark.parametrize(
    ("mode", "expected", "disposition"),
    (
        ("before-replace", b"old\n", "recovered_old"),
        ("after-replace", b"new\n", "verified_new"),
    ),
)
def test_sigkill_recovery_yields_complete_old_or_new_tree(
    tmp_path, mode, expected, disposition
) -> None:
    """Catches restart recovery losing the last durable intent boundary."""
    worker = Path(__file__).parent / "helpers" / "transaction_crash_worker.py"
    result = subprocess.run(
        [sys.executable, str(worker), str(tmp_path), mode], capture_output=True, text=True
    )
    assert result.returncode != 0

    recovery = recover_transactions(tmp_path)

    assert recovery[0].disposition == disposition
    assert (tmp_path / "main.tf").read_bytes() == expected
    assert not (tmp_path / ".ubitofu" / "transactions").exists()


@pytest.mark.parametrize(
    ("boundary", "expected", "disposition"),
    (
        ("manifest-publish-before", b"old\n", "recovered_old"),
        ("manifest-published", b"old\n", "recovered_old"),
        ("journal-publish-before", b"old\n", "recovered_old"),
        ("journal-prepared", b"old\n", "recovered_old"),
        ("intent-publish-before", b"old\n", "recovered_old"),
        ("intent-published", b"old\n", "recovered_old"),
        ("applied-publish-before", b"new\n", "verified_new"),
        ("applied-published", b"new\n", "verified_new"),
        ("committed-publish-before", b"new\n", "verified_new"),
        ("committed-published", b"new\n", "verified_new"),
        ("cleanup-start", b"new\n", "verified_new"),
    ),
)
def test_sigkill_recovery_at_each_durable_publication(
    tmp_path, boundary, expected, disposition
) -> None:
    """Catches a durable phase publication without a deterministic restart result."""
    worker = Path(__file__).parent / "helpers" / "transaction_crash_worker.py"
    result = subprocess.run(
        [sys.executable, str(worker), str(tmp_path), boundary],
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0

    recovery = recover_transactions(tmp_path)

    assert recovery[0].disposition == disposition
    assert (tmp_path / "main.tf").read_bytes() == expected
    assert not (tmp_path / ".ubitofu" / "transactions").exists()


def test_sigkill_during_rollback_replacement_recovers_complete_old_tree(
    tmp_path,
) -> None:
    """Catches a crash after rollback replace but before rollback cleanup."""
    worker = Path(__file__).parent / "helpers" / "transaction_crash_worker.py"
    result = subprocess.run(
        [sys.executable, str(worker), str(tmp_path), "rollback-replaced"],
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0

    recovery = recover_transactions(tmp_path)

    assert recovery[0].disposition == "recovered_old"
    assert (tmp_path / "a.tf").read_bytes() == b"old a\n"
    assert (tmp_path / "b.tf").read_bytes() == b"old b\n"


def test_recovery_removes_only_journaled_temp_and_keeps_neighbor(tmp_path) -> None:
    """Catches cleanup by glob that deletes a neighboring administrator file."""
    neighbor = tmp_path / ".ubitofu-neighbor.tmp"
    neighbor.write_bytes(b"keep\n")
    worker = Path(__file__).parent / "helpers" / "transaction_crash_worker.py"
    subprocess.run(
        [sys.executable, str(worker), str(tmp_path), "before-replace"],
        check=False,
    )

    recover_transactions(tmp_path)

    assert neighbor.read_bytes() == b"keep\n"


def test_manifest_temp_name_cannot_be_redirected_to_a_neighbor(tmp_path) -> None:
    """Catches a permissive manifest redirecting exact-name cleanup to another file."""
    path = tmp_path / "main.tf"
    path.write_bytes(b"old\n")
    prepare_transaction(
        workdir=tmp_path, files=(_proposed(tmp_path, "main.tf", b"new\n"),)
    )
    root = _transaction_root(tmp_path)
    manifest = root / "manifest.json"
    document = json.loads(manifest.read_text())
    document["entries"][0]["destination_temp_name"] = ".ubitofu-neighbor.tmp"
    manifest.write_text(json.dumps(document))
    neighbor = tmp_path / ".ubitofu-neighbor.tmp"
    neighbor.write_bytes(b"new\n")

    with pytest.raises(UbitofuError):
        recover_transactions(tmp_path)
    assert neighbor.read_bytes() == b"new\n"
    assert root.exists()


def test_unexpected_journaled_temp_content_quarantines_without_deletion(tmp_path) -> None:
    """Catches recovery deleting a journaled name whose bytes are not its candidate."""
    worker = Path(__file__).parent / "helpers" / "transaction_crash_worker.py"
    subprocess.run(
        [sys.executable, str(worker), str(tmp_path), "before-replace"], check=False
    )
    root = _transaction_root(tmp_path)
    manifest = json.loads((root / "manifest.json").read_text())
    temporary = tmp_path / manifest["entries"][0]["destination_temp_name"]
    temporary.write_bytes(b"third party temp\n")

    recovery = recover_transactions(tmp_path)

    assert recovery[0].disposition == "quarantined"
    assert temporary.read_bytes() == b"third party temp\n"
    assert (tmp_path / "main.tf").read_bytes() == b"old\n"


def test_recovery_recognizes_restored_old_bytes_after_rollback_replace(tmp_path) -> None:
    """Catches requiring the pre-commit inode after a durable rollback replacement."""
    worker = Path(__file__).parent / "helpers" / "transaction_crash_worker.py"
    subprocess.run(
        [sys.executable, str(worker), str(tmp_path), "after-replace"], check=False
    )
    restored = tmp_path / "restored.tmp"
    restored.write_bytes(b"old\n")
    os.chmod(restored, 0o644)
    os.replace(restored, tmp_path / "main.tf")

    recovery = recover_transactions(tmp_path)

    assert recovery[0].disposition == "recovered_old"
    assert (tmp_path / "main.tf").read_bytes() == b"old\n"


def test_recovery_quarantines_candidate_bytes_with_wrong_mode(tmp_path) -> None:
    """Catches treating content digest alone as a complete candidate tree."""
    worker = Path(__file__).parent / "helpers" / "transaction_crash_worker.py"
    subprocess.run(
        [sys.executable, str(worker), str(tmp_path), "after-replace"], check=False
    )
    os.chmod(tmp_path / "main.tf", 0o600)

    recovery = recover_transactions(tmp_path)

    assert recovery[0].disposition == "quarantined"
    assert (tmp_path / "main.tf").read_bytes() == b"new\n"


def test_ambiguous_recovery_quarantines_and_runtime_blocks(tmp_path) -> None:
    """Catches overwriting a third-party value that matches neither old nor new."""
    worker = Path(__file__).parent / "helpers" / "transaction_crash_worker.py"
    subprocess.run(
        [sys.executable, str(worker), str(tmp_path), "after-replace"], check=False
    )
    (tmp_path / "main.tf").write_bytes(b"third party\n")

    recovery = recover_transactions(tmp_path)

    assert recovery[0].disposition == "quarantined"
    assert (tmp_path / "main.tf").read_bytes() == b"third party\n"
    with pytest.raises(UbitofuError):
        with runtime_session(tmp_path):
            pass
    assert _transaction_root(tmp_path).exists()


def test_runtime_recovers_prepared_transaction_before_yielding(tmp_path) -> None:
    """Catches source snapshots starting before residual transaction recovery."""
    path = tmp_path / "main.tf"
    path.write_bytes(b"old\n")
    prepare_transaction(
        workdir=tmp_path, files=(_proposed(tmp_path, "main.tf", b"new\n"),)
    )

    with runtime_session(tmp_path):
        assert path.read_bytes() == b"old\n"
        assert not (tmp_path / ".ubitofu" / "transactions").exists()
