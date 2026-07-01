import subprocess
import tomllib
from collections.abc import Callable, Mapping
from dataclasses import dataclass


@dataclass
class Config:
    controller_url: str
    site: str
    api_key_source: str
    api_key_ref: str
    op_vault: str = "ExampleVault"
    workdir: str = "."


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
