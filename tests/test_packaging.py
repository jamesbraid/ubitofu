# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 James Braid
"""Distribution-boundary checks for runtime parser dependencies."""

from __future__ import annotations

import os
import subprocess
import sys
import tomllib
import venv
from pathlib import Path


def test_built_wheel_and_sdist_install_the_public_cli_in_clean_environments(tmp_path) -> None:
    """Catches source-checkout imports that hide a broken distribution boundary."""
    repository = Path(__file__).resolve().parents[1]
    project = tomllib.loads((repository / "pyproject.toml").read_text())["project"]
    expected_version = project["version"]
    clean_env = os.environ.copy()
    clean_env.pop("PYTHONPATH", None)
    dist = tmp_path / "dist"
    subprocess.run(
        [sys.executable, "-m", "build", "--no-isolation", "--outdir", str(dist)],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
        env=clean_env,
    )
    artifacts = (next(dist.glob("ubitofu-*.whl")), next(dist.glob("ubitofu-*.tar.gz")))
    for index, artifact in enumerate(artifacts):
        environment = tmp_path / f"clean-{index}"
        venv.EnvBuilder(with_pip=True).create(environment)
        python = environment / "bin" / "python"
        subprocess.run(
            [str(python), "-m", "pip", "install", str(artifact)],
            check=True,
            capture_output=True,
            text=True,
            env=clean_env,
        )
        subprocess.run(
            [
                str(python),
                "-c",
                "import tree_sitter, tree_sitter_hcl, ubitofu; "
                f"assert ubitofu.__version__ == {expected_version!r}",
            ],
            check=True,
            capture_output=True,
            text=True,
            env=clean_env,
        )
        help_result = subprocess.run(
            [str(environment / "bin" / "ubitofu"), "--help"],
            check=True,
            capture_output=True,
            text=True,
            env=clean_env,
        )
        assert "reconcile" in help_result.stdout
        assert "enumerate" not in help_result.stdout
