# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 James Braid
"""Crash a real transaction at one named durable boundary."""

from __future__ import annotations

import hashlib
import json
import os
import signal
import sys
from pathlib import Path, PurePosixPath

import ubitofu.file_transaction as file_transaction
from ubitofu.file_metadata import inspect_file_metadata
from ubitofu.file_transaction import TransactionPhase, prepare_transaction
from ubitofu.reconcile_renderer import ProposedFile


def _kill() -> None:
    os.kill(os.getpid(), signal.SIGKILL)


def _proposed(workdir: Path, name: str, old: bytes, new: bytes) -> ProposedFile:
    path = workdir / name
    path.write_bytes(old)
    identity = inspect_file_metadata(
        path, relative_path=PurePosixPath(name)
    ).identity
    return ProposedFile(
        PurePosixPath(name),
        identity,
        new,
        hashlib.sha256(new).hexdigest(),
        identity.mode,
    )


def main() -> int:
    workdir = Path(sys.argv[1])
    boundary = sys.argv[2]
    if boundary == "rollback-replaced":
        proposed = (
            _proposed(workdir, "a.tf", b"old a\n", b"new a\n"),
            _proposed(workdir, "b.tf", b"old b\n", b"new b\n"),
        )
        primary_destination = workdir / "a.tf"
    else:
        proposed = (_proposed(workdir, "main.tf", b"old\n", b"new\n"),)
        primary_destination = workdir / "main.tf"

    if boundary == "manifest-published":
        real_write_manifest = file_transaction._write_manifest

        def crash_after_manifest(transaction):
            real_write_manifest(transaction)
            _kill()

        file_transaction._write_manifest = crash_after_manifest

    real_write_journal = file_transaction._write_journal

    def crash_at_journal(transaction, journal):
        real_write_journal(transaction, journal)
        if boundary == "journal-prepared" and journal.phase is TransactionPhase.PREPARED:
            _kill()
        if (
            boundary == "intent-published"
            and journal.phase is TransactionPhase.COMMITTING
            and journal.intents
            and not journal.applied
        ):
            _kill()
        if (
            boundary == "applied-published"
            and journal.phase is TransactionPhase.COMMITTING
            and journal.applied
        ):
            _kill()
        if boundary == "committed-published" and journal.phase is TransactionPhase.COMMITTED:
            _kill()

    file_transaction._write_journal = crash_at_journal
    real_replace = os.replace

    def crash(source, destination):
        target = Path(destination)
        if target.name == "manifest.json" and boundary == "manifest-publish-before":
            _kill()
        if target.name == "journal.json" and boundary.endswith("publish-before"):
            document = json.loads(Path(source).read_bytes())
            phase = document["phase"]
            intents = document["intents"]
            applied = document["applied"]
            if boundary == "journal-publish-before" and phase == "prepared":
                _kill()
            if boundary == "intent-publish-before" and intents and not applied:
                _kill()
            if boundary == "applied-publish-before" and applied and phase == "committing":
                _kill()
            if boundary == "committed-publish-before" and phase == "committed":
                _kill()
        if boundary == "rollback-replaced" and target == workdir / "b.tf":
            raise OSError("synthetic second replacement failure")
        if target == primary_destination:
            if boundary == "before-replace":
                _kill()
            real_replace(source, destination)
            if boundary == "after-replace":
                _kill()
            return
        real_replace(source, destination)

    os.replace = crash  # type: ignore[assignment]
    transaction = prepare_transaction(workdir=workdir, files=proposed)

    if boundary == "cleanup-start":

        def crash_before_cleanup(transaction):
            _kill()

        file_transaction._cleanup_transaction = crash_before_cleanup

    if boundary == "rollback-replaced":
        real_restore_old = file_transaction._restore_old

        def crash_after_rollback(transaction, entry):
            real_restore_old(transaction, entry)
            if entry.relative_path == PurePosixPath("a.tf"):
                _kill()

        file_transaction._restore_old = crash_after_rollback

    transaction.commit()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
