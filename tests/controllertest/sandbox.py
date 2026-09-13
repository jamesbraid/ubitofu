# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 James Braid
"""Per-scenario tofu + ubitofu sandbox.

`apply()` is the deliberate test-only exception to ubitofu's never-apply
rule: scenarios need real state, and the target controller is a disposable
container. Production code paths under test never apply.
"""
import os
import re
import subprocess
from pathlib import Path

from .pins import PROVIDER_SOURCE, PROVIDER_VERSION
from .support import RunningController

_PROVIDERS_TF = """\
terraform {{
  required_providers {{
    unifi = {{
      source  = "{provider_source}"
      version = "{provider_version}"
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
password_ref = "{password_var}"
verify_tls = false
workdir = "{workdir}"
"""


class Sandbox:
    def __init__(self, workdir: Path, controller: RunningController, site: str,
                 plugin_cache: Path, monkeypatch) -> None:
        self.workdir = workdir
        password_var = f"UNIFI_TEST_PASSWORD_{re.sub(r'[^A-Za-z0-9]', '_', site).upper()}"
        monkeypatch.setenv(password_var, controller.password)
        self._env = {**os.environ, "TF_PLUGIN_CACHE_DIR": str(plugin_cache)}
        workdir.mkdir(parents=True, exist_ok=True)
        (workdir / "providers.tf").write_text(_PROVIDERS_TF.format(
            api_url=controller.base_url, username=controller.username,
            password=controller.password, site=site,
            provider_source=os.environ.get("UNIFI_TEST_PROVIDER_SOURCE", PROVIDER_SOURCE),
            provider_version=os.environ.get("UNIFI_TEST_PROVIDER_VERSION", PROVIDER_VERSION),
        ))
        self.config_path = workdir / "config.toml"
        self.config_path.write_text(_CONFIG_TOML.format(
            api_url=controller.base_url, username=controller.username,
            site=site, workdir=workdir, password_var=password_var,
        ))

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

    def ubitofu(self, *args: str) -> int:
        from ubitofu.cli import main
        return main(
            [*args, "--config", str(self.config_path), "--format", "json"]
        )

    def generation_preview(self):
        """Collect the exact internal preview without committing candidates.

        Live controller scenarios use this when the pinned simulator has known
        coverage gaps. The snapshot remains inspectable, while the production
        rule still suppresses every candidate and write.
        """
        from ubitofu.config import load_config
        from ubitofu.controller import controller_from_config
        from ubitofu.generate import prepare_generate
        from ubitofu.runtime import runtime_session
        from ubitofu.tofu_runner import TofuRunner

        cfg = load_config(str(self.config_path))
        with runtime_session(self.workdir) as session:
            controller = controller_from_config(cfg)
            try:
                return prepare_generate(
                    cfg=cfg,
                    controller=controller,
                    runner=TofuRunner(workdir=session.workdir),
                    session=session,
                )
            finally:
                controller.close()


_NATIVE_PROVIDERS_TF = """\
terraform {{
  required_providers {{
    unifi = {{
      source  = "{provider_source}"
      version = "{provider_version}"
    }}
  }}
}}

provider "unifi" {{
  api_url        = "{api_url}"
  api_key        = "{api_key}"
  site           = "{site}"
  allow_insecure = true
}}
"""

_NATIVE_CONFIG_TOML = """\
controller_url = "{api_url}"
site = "{site}"
api_key_source = "env"
api_key_ref = "{key_var}"
verify_tls = false
workdir = "{workdir}"
"""


def native_workspace(
    workdir: Path, controller: RunningController, site: str, monkeypatch,
    *, key_var: str = "UNIFI_TEST_UOS_KEY",
) -> Path:
    """Set up a UOS native-dialect (X-API-KEY) ubitofu workspace: config.toml,
    a provider block, and `tofu init`. ubitofu generates resource/import blocks
    into the workspace but does not write the provider block, so the workspace
    supplies it. controller.api_key is the seeded UOS's baked key. Returns the
    config path.
    """
    workdir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv(key_var, controller.api_key)
    (workdir / "providers.tf").write_text(_NATIVE_PROVIDERS_TF.format(
        api_url=controller.base_url, api_key=controller.api_key, site=site,
        provider_source=os.environ.get("UNIFI_TEST_PROVIDER_SOURCE", PROVIDER_SOURCE),
        provider_version=os.environ.get("UNIFI_TEST_PROVIDER_VERSION", PROVIDER_VERSION),
    ))
    cfg = workdir / "config.toml"
    cfg.write_text(_NATIVE_CONFIG_TOML.format(
        api_url=controller.base_url, site=site, key_var=key_var, workdir=workdir))
    subprocess.run(["tofu", "init", "-input=false"], cwd=str(workdir), check=True,
                   capture_output=True)
    return cfg
