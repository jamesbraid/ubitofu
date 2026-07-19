# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 James Braid
import subprocess
import tomllib
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path


@dataclass
class Config:
    controller_url: str
    site: str
    api_key_source: str
    api_key_ref: str
    op_vault: str  # required: the operator's secret-manager vault, from config
    workdir: str = "."

    def __post_init__(self) -> None:
        # TofuRunner uses workdir as tofu's cwd while the pipelines pass
        # workdir-prefixed output paths on the command line; a relative
        # workdir makes tofu resolve those paths from inside the workdir
        # itself ("./work" -> work/work/tf.plan). Absolutize once here so
        # the cwd and every path built from workdir agree.
        self.workdir = str(Path(self.workdir).resolve())


def load_config(path: str) -> Config:
    with open(path, "rb") as fh:
        data = tomllib.load(fh)
    return Config(**data)


def _op_read(ref: str) -> str:
    result = subprocess.run(["op", "read", ref], capture_output=True, text=True, check=True)
    return result.stdout.strip()


def resolve_api_key(
    cfg: Config,
    environ: Mapping[str, str],
    op_reader: Callable[[str], str] = _op_read,
) -> str:
    if cfg.api_key_source == "env":
        return environ[cfg.api_key_ref]
    if cfg.api_key_source == "op":
        return op_reader(cfg.api_key_ref)
    raise ValueError(f"unknown api_key_source: {cfg.api_key_source!r}")
