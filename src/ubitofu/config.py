# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 James Braid
import subprocess
import tomllib
from collections.abc import Callable, Mapping
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Literal


@dataclass
class Config:
    controller_url: str
    site: str
    api_key_source: str = ""
    api_key_ref: str = ""
    op_vault: str = ""  # the operator's secret-manager vault, from config
    workdir: str = "."
    # Classic (self-hosted) dialect: cookie login instead of X-API-KEY.
    dialect: str = "unifi-os"
    username: str = ""
    password_source: str = ""
    password_ref: str = ""
    verify_tls: bool = True
    ca_bundle: str = ""
    provider_contract: str = ""
    provider_contract_checksum: str = ""
    provider_binary: str = ""
    provider_schema_cli: str = ""

    def __post_init__(self) -> None:
        # TofuRunner uses workdir as tofu's cwd while the pipelines pass
        # workdir-prefixed output paths on the command line; a relative
        # workdir makes tofu resolve those paths from inside the workdir
        # itself ("./work" -> work/work/tf.plan). Absolutize once here so
        # the cwd and every path built from workdir agree.
        self.workdir = str(Path(self.workdir).resolve())


class ConfigError(ValueError):
    """Config is structurally loadable but cross-field invalid.

    Raised by load_config (never by Config() itself — direct construction
    stays unvalidated so tests and library callers can build partial
    configs). Every message names the dialect and the offending/missing
    key(s) so the CLI can print one actionable line instead of the
    ValueError a resolver would raise deep inside a run.
    """


class CredentialConfigError(ConfigError):
    """A configured secret source cannot provide the selected credential."""

    def __init__(self, credential: Literal["api_key", "password"]) -> None:
        super().__init__("configured credential is unavailable")
        self.credential = credential


class TLSConfigError(ConfigError):
    """The configured TLS trust material cannot be loaded."""


def _classic_missing(cfg: Config) -> list[str]:
    missing = []
    if not cfg.username:
        missing.append("username")
    source_bad = cfg.password_source not in ("env", "op")
    ref_bad = not cfg.password_ref
    if source_bad and ref_bad:
        missing.append("password_source/password_ref")
    elif source_bad:
        missing.append("password_source")
    elif ref_bad:
        missing.append("password_ref")
    return missing


def _unifi_os_missing(cfg: Config) -> list[str]:
    missing = []
    source_bad = cfg.api_key_source not in ("env", "op")
    ref_bad = not cfg.api_key_ref
    if source_bad and ref_bad:
        missing.append("api_key_source/api_key_ref")
    elif source_bad:
        missing.append("api_key_source")
    elif ref_bad:
        missing.append("api_key_ref")
    return missing


def validate_config(cfg: Config) -> None:
    """Validate cross-field constraints on the complete config document."""
    if cfg.dialect not in ("unifi-os", "classic"):
        raise ConfigError(f'dialect {cfg.dialect!r} must be "unifi-os" or "classic"')
    if not isinstance(cfg.verify_tls, bool):
        raise ConfigError("verify_tls must be true or false")
    if cfg.ca_bundle:
        if not cfg.verify_tls:
            raise ConfigError("ca_bundle cannot be used when verify_tls is false")
        if not Path(cfg.ca_bundle).is_file():
            raise ConfigError("ca_bundle must name an existing regular file")
    provider_bundle = {
        "provider_contract": cfg.provider_contract,
        "provider_contract_checksum": cfg.provider_contract_checksum,
        "provider_binary": cfg.provider_binary,
        "provider_schema_cli": cfg.provider_schema_cli,
    }
    configured = {name for name, value in provider_bundle.items() if value}
    if configured and len(configured) != len(provider_bundle):
        missing_fields = ", ".join(sorted(set(provider_bundle) - configured))
        raise ConfigError(f"provider contract bundle is incomplete: {missing_fields}")
    if cfg.dialect == "classic":
        missing = _classic_missing(cfg)
        if missing:
            raise ConfigError(f'dialect "classic" requires {" and ".join(missing)}')
    else:
        missing = _unifi_os_missing(cfg)
        if missing:
            raise ConfigError(f'dialect "unifi-os" requires {" and ".join(missing)}')
    for name, source in (
        ("api_key_source", cfg.api_key_source),
        ("password_source", cfg.password_source),
    ):
        if source == "op" and not cfg.op_vault:
            raise ConfigError(
                f'{name} is "op" but op_vault is empty — set op_vault in config'
            )


def load_config(path: str, validate: bool = True) -> Config:
    with open(path, "rb") as fh:
        data = tomllib.load(fh)
    unknown = sorted(set(data) - {field.name for field in fields(Config)})
    if unknown:
        raise ConfigError(f"unknown configuration field: {', '.join(unknown)}")
    config_dir = Path(path).resolve().parent
    for name in (
        "provider_contract",
        "provider_contract_checksum",
        "provider_binary",
        "provider_schema_cli",
    ):
        value = data.get(name)
        if value:
            if name == "provider_schema_cli" and "/" not in value:
                continue
            candidate = Path(value)
            data[name] = str(candidate if candidate.is_absolute() else config_dir / candidate)
    cfg = Config(**data)
    if validate:
        validate_config(cfg)
    return cfg


def _op_read(ref: str) -> str:
    result = subprocess.run(["op", "read", ref], capture_output=True, text=True, check=True)
    return result.stdout.strip()


def resolve_api_key(
    cfg: Config,
    environ: Mapping[str, str],
    op_reader: Callable[[str], str] = _op_read,
) -> str:
    if cfg.api_key_source == "env":
        try:
            return environ[cfg.api_key_ref]
        except KeyError:
            raise CredentialConfigError("api_key") from None
    if cfg.api_key_source == "op":
        return op_reader(cfg.api_key_ref)
    raise ValueError(f"unknown api_key_source: {cfg.api_key_source!r}")


def resolve_password(
    cfg: Config,
    environ: Mapping[str, str],
    op_reader: Callable[[str], str] = _op_read,
) -> str:
    if cfg.password_source == "env":
        try:
            return environ[cfg.password_ref]
        except KeyError:
            raise CredentialConfigError("password") from None
    if cfg.password_source == "op":
        return op_reader(cfg.password_ref)
    raise ValueError(f"unknown password_source: {cfg.password_source!r}")
