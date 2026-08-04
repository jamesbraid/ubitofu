# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 James Braid
"""Crash startup recovery before publishing its committed-cleanup marker."""

from __future__ import annotations

import os
import signal
import sys
from pathlib import Path

from ubitofu.file_transaction import recover_transactions


def main() -> int:
    workdir = Path(sys.argv[1])
    real_replace = os.replace

    def crash_before_cleanup_marker(source, destination):
        if Path(destination).name.startswith("cleanup-"):
            os.kill(os.getpid(), signal.SIGKILL)
        real_replace(source, destination)

    os.replace = crash_before_cleanup_marker  # type: ignore[assignment]
    recover_transactions(workdir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
