# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 James Braid
"""Crash a real transaction at a destination replacement boundary."""

from __future__ import annotations

import hashlib
import os
import signal
import sys
from pathlib import Path, PurePosixPath

from ubitofu.file_metadata import inspect_file_metadata
from ubitofu.file_transaction import prepare_transaction
from ubitofu.reconcile_renderer import ProposedFile


def main() -> int:
    workdir = Path(sys.argv[1])
    mode = sys.argv[2]
    path = workdir / "main.tf"
    path.write_bytes(b"old\n")
    identity = inspect_file_metadata(
        path, relative_path=PurePosixPath("main.tf")
    ).identity
    candidate = b"new\n"
    transaction = prepare_transaction(
        workdir=workdir,
        files=(
            ProposedFile(
                PurePosixPath("main.tf"),
                identity,
                candidate,
                hashlib.sha256(candidate).hexdigest(),
                identity.mode,
            ),
        ),
    )
    real_replace = os.replace

    def crash(source, destination):
        if Path(destination) == path:
            if mode == "before-replace":
                os.kill(os.getpid(), signal.SIGKILL)
            real_replace(source, destination)
            os.kill(os.getpid(), signal.SIGKILL)
        real_replace(source, destination)

    os.replace = crash  # type: ignore[assignment]
    transaction.commit()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
