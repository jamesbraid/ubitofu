# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 James Braid
"""Per-scenario tofu + ubitofu sandbox.

`apply()` is the deliberate test-only exception to ubitofu's never-apply
rule: scenarios need real state, and the target controller is a disposable
container. Production code paths under test never apply.
"""
import os
import subprocess
from pathlib import Path

from .support import RunningController

_PROVIDERS_TF = """\
terraform {{
  required_providers {{
    unifi = {{
      source = "ubiquiti-community/unifi"
    }}
  }}
}}

provider "unifi" {{
  api_url        = "{api_url}"
  username       = "{username}"
  password       = "{password}"
  site           = "{site}"
  allow_insecure = true
}}
"""

_CONFIG_TOML = """\
controller_url = "{api_url}"
site = "{site}"
dialect = "classic"
username = "{username}"
password_source = "env"
password_ref = "UNIFI_TEST_PASSWORD"
workdir = "{workdir}"
"""


class Sandbox:
    def __init__(self, workdir: Path, controller: RunningController, site: str,
                 plugin_cache: Path, monkeypatch) -> None:
        self.workdir = workdir
        self._env = {**os.environ, "TF_PLUGIN_CACHE_DIR": str(plugin_cache)}
        workdir.mkdir(parents=True, exist_ok=True)
        (workdir / "providers.tf").write_text(_PROVIDERS_TF.format(
            api_url=controller.base_url, username=controller.username,
            password=controller.password, site=site,
        ))
        self.config_path = workdir / "config.toml"
        self.config_path.write_text(_CONFIG_TOML.format(
            api_url=controller.base_url, username=controller.username,
            site=site, workdir=workdir,
        ))
        monkeypatch.setenv("UNIFI_TEST_PASSWORD", controller.password)

    def tofu(self, *args: str) -> subprocess.CompletedProcess[str]:
        proc = subprocess.run(
            ["tofu", *args], cwd=str(self.workdir), env=self._env,
            capture_output=True, text=True,
        )
        if proc.returncode != 0:
            raise RuntimeError(
                f"tofu {' '.join(args)} exited {proc.returncode}:\n{proc.stderr}"
            )
        return proc

    def init(self) -> None:
        self.tofu("init", "-input=false")

    def apply(self) -> None:
        self.tofu("apply", "-auto-approve", "-input=false")

    def ubitofu(self, command: str) -> int:
        from ubitofu.cli import main
        return main([command, "--config", str(self.config_path)])
