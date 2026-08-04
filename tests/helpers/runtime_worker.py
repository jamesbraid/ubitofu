# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 James Braid
import sys
import time
from pathlib import Path

from ubitofu.runtime import RuntimeBusyError, runtime_session


def main() -> int:
    workdir = Path(sys.argv[1])
    nonblocking = len(sys.argv) > 2
    try:
        with runtime_session(workdir, blocking=not nonblocking):
            print("locked", flush=True)
            time.sleep(30)
    except RuntimeBusyError:
        print("blocked", flush=True)
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
