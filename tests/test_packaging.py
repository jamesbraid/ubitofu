# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 James Braid
"""Distribution-boundary checks for runtime parser dependencies."""

from __future__ import annotations

import subprocess
import sys
import venv
from pathlib import Path


def test_built_wheel_installs_the_tree_sitter_runtime_in_a_clean_environment(tmp_path) -> None:
    """Catches a source checkout import that hides missing wheel dependencies."""
    repository = Path(__file__).resolve().parents[1]
    dist = tmp_path / "dist"
    subprocess.run(
        [sys.executable, "-m", "build", "--wheel", "--no-isolation", "--outdir", str(dist)],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    )
    environment = tmp_path / "clean"
    venv.EnvBuilder(with_pip=True).create(environment)
    python = environment / "bin" / "python"
    wheel = next(dist.glob("ubitofu-*.whl"))
    subprocess.run(
        [str(python), "-m", "pip", "install", str(wheel)],
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        [str(python), "-c", "import tree_sitter, tree_sitter_hcl"],
        check=True,
        capture_output=True,
        text=True,
    )
