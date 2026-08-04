# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 James Braid
import sys
import time
from pathlib import Path

from ubitofu.runtime import (
    RuntimeBusyError,
    generation_import_scaffold,
    runtime_session,
)


def main() -> int:
    workdir = Path(sys.argv[1])
    mode = sys.argv[2] if len(sys.argv) > 2 else ""
    nonblocking = mode == "nonblocking"
    try:
        with runtime_session(workdir, blocking=not nonblocking) as session:
            if mode == "scaffold":
                with generation_import_scaffold(
                    session,
                    b'import {\n  to = terraform_data.example\n  id = "synthetic"\n}\n',
                ):
                    print("locked", flush=True)
                    time.sleep(30)
            else:
                print("locked", flush=True)
                time.sleep(30)
    except RuntimeBusyError:
        print("blocked", flush=True)
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
