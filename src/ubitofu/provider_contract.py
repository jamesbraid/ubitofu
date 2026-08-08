# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 James Braid
"""Fail-closed admission of one provider execution environment."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import Config
from .errors import ProviderContractError, UbitofuError
from .manifest import ResourceSpec, spec_for_type
from .tofu_runner import TofuRunner

_PROVIDER_ADDRESS = "registry.terraform.io/ubiquiti-community/unifi"
_BUNDLE_FIELDS = (
    "provider_contract",
    "provider_contract_checksum",
    "provider_binary",
    "provider_schema_cli",
)


@dataclass(frozen=True)
class ProviderContract:
    contract_id: str
    mode: str
    resource_spec: ResourceSpec
    catalog_sha256: str
    lifecycle_receipt_sha256: str
    sidecar_sha256: str


@dataclass
class ProviderExecution:
    """The admitted schema and runner inputs for one command execution."""

    contract: ProviderContract | None
    schema: dict[str, Any] | None
    binary: str = "tofu"
    environment: Mapping[str, str] | None = None
    _scope: tempfile.TemporaryDirectory[str] | None = None

    def runner(self, *, workdir: Path, plan_path: Path | None = None) -> TofuRunner:
        return TofuRunner(
            workdir=workdir,
            binary=self.binary,
            environment=self.environment,
            plan_path=plan_path,
            cached_provider_schema=self.schema,
        )

    def cleanup(self) -> None:
        if self._scope is not None:
            self._scope.cleanup()
            self._scope = None


@dataclass(frozen=True)
class _StaticContractEvidence:
    contract: ProviderContract
    provider_sha256: str
    toolchains: Mapping[str, object]


@dataclass(frozen=True)
class _SchemaEvidence:
    version: str
    canonical_schema_sha256: str


def _fail(reason: str) -> ProviderContractError:
    return ProviderContractError(f"provider contract evidence is invalid: {reason}")


def _sha256(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as exc:
        raise _fail("required file is unavailable") from exc


def _mapping(value: object) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise _fail("contract document is malformed")
    return value


def _string(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise _fail("contract document is malformed")
    return value


def _canonical_schema(schema: Mapping[str, object], provider_address: str) -> bytes:
    """Encode only the selected provider as the contract's canonical bytes."""
    providers = _mapping(schema.get("provider_schemas"))
    projection = _mapping(providers.get(provider_address))
    _mapping(projection.get("provider"))
    try:
        encoded = json.dumps(
            projection,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise _fail("provider schema is malformed") from exc
    encoded = (
        encoded.replace("&", "\\u0026")
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )
    return (encoded + "\n").encode()


def _sidecar_digest(path: Path, contract: Path) -> str:
    try:
        pieces = path.read_text().strip().split()
    except OSError as exc:
        raise _fail("sidecar checksum is unavailable") from exc
    if len(pieces) != 2 or pieces[1] != contract.name:
        raise _fail("sidecar checksum is malformed")
    digest = pieces[0]
    if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest.lower()):
        raise _fail("sidecar checksum is malformed")
    return digest


def _cli_name(path: Path) -> str:
    name = path.name.lower()
    if name.startswith("terraform"):
        return "terraform"
    if name.startswith(("tofu", "opentofu")):
        return "tofu"
    raise _fail("provider schema CLI is unsupported")


def _executable(value: str) -> Path:
    found = shutil.which(value)
    candidate = Path(found if found is not None else value)
    try:
        path = candidate.resolve(strict=True)
    except OSError as exc:
        raise _fail("provider schema CLI is unavailable") from exc
    if not path.is_file() or not os.access(path, os.X_OK):
        raise _fail("provider schema CLI is unavailable")
    return path


def _selected_provider(scope: Path, value: str) -> tuple[Path, Path]:
    try:
        source = Path(value).resolve(strict=True)
        if not source.is_file():
            raise OSError
        provider_dir = scope / "provider"
        provider_dir.mkdir(mode=0o700)
        selected = provider_dir / source.name
        shutil.copyfile(source, selected)
        selected.chmod(0o555)
    except OSError as exc:
        raise _fail("provider binary is unavailable") from exc
    return provider_dir, selected


def _read_contract(path: Path) -> Mapping[str, object]:
    try:
        return _mapping(json.loads(path.read_text()))
    except (OSError, json.JSONDecodeError) as exc:
        raise _fail("contract document is unreadable") from exc


def _verify_static_contract(
    *,
    contract_path: Path,
    checksum_path: Path,
    provider_binary: Path,
) -> _StaticContractEvidence:
    expected_sidecar = _sidecar_digest(checksum_path, contract_path)
    actual_sidecar = _sha256(contract_path)
    if actual_sidecar != expected_sidecar:
        raise _fail("sidecar checksum does not match contract")
    root = _read_contract(contract_path)
    if root.get("format_version") != 1 or root.get("mode") != "provider_projection_required":
        raise _fail("contract format is unsupported")
    contract_id = _string(root.get("contract_id"))
    provider = _mapping(root.get("provider"))
    if provider.get("address") != _PROVIDER_ADDRESS:
        raise _fail("provider identity does not match")
    binary = _mapping(provider.get("binary"))
    provider_sha256 = _string(binary.get("sha256"))
    if provider_sha256 != _sha256(provider_binary):
        raise _fail("provider binary mismatch")
    schema_identity = _mapping(provider.get("schema"))
    toolchains = _mapping(schema_identity.get("toolchains"))
    resource = _mapping(root.get("resource"))
    try:
        expected_spec = spec_for_type(_string(resource.get("resource_type")))
    except KeyError as exc:
        raise _fail("manifest identity does not match") from exc
    identity = (
        resource.get("resource_type"),
        resource.get("endpoint"),
        resource.get("id_rule"),
        resource.get("site_scoped"),
    )
    if identity != (
        expected_spec.resource_type,
        expected_spec.endpoint,
        expected_spec.id_rule,
        expected_spec.site_scoped,
    ):
        raise _fail("manifest identity does not match")
    redaction = _mapping(resource.get("redaction"))
    if (
        resource.get("capture_eligible") is not True
        or redaction.get("schema_sensitive") is not True
        or redaction.get("secret_shaped") is not True
    ):
        raise _fail("contract lifecycle identity does not match")
    lifecycle = _mapping(root.get("lifecycle"))
    if lifecycle.get("result") != "pass":
        raise _fail("contract lifecycle identity does not match")
    return _StaticContractEvidence(
        contract=ProviderContract(
            contract_id=contract_id,
            mode="provider_projection_required",
            resource_spec=expected_spec,
            catalog_sha256=_string(_mapping(root.get("catalog")).get("sha256")),
            lifecycle_receipt_sha256=_string(lifecycle.get("receipt_sha256")),
            sidecar_sha256=actual_sidecar,
        ),
        provider_sha256=provider_sha256,
        toolchains=toolchains,
    )


def _verify_schema_evidence(
    *, toolchains: Mapping[str, object], cli: Path
) -> _SchemaEvidence:
    toolchain = _mapping(toolchains.get(_cli_name(cli)))
    if _string(toolchain.get("binary_sha256")) != _sha256(cli):
        raise _fail("schema CLI mismatch")
    return _SchemaEvidence(
        version=_string(toolchain.get("version")),
        canonical_schema_sha256=_string(toolchain.get("canonical_schema_sha256")),
    )


def _configured(cfg: Config) -> bool:
    values = {name: getattr(cfg, name) for name in _BUNDLE_FIELDS}
    if any(not isinstance(value, str) for value in values.values()):
        raise _fail("provider contract values must be non-empty strings")
    present = {name for name, value in values.items() if value}
    if present and len(present) != len(values):
        raise _fail("provider contract bundle is incomplete")
    return bool(present)


def _admit(*, cfg: Config, workdir: Path) -> ProviderExecution:
    if not _configured(cfg):
        return ProviderExecution(contract=None, schema=None)
    try:
        static = _verify_static_contract(
            contract_path=Path(cfg.provider_contract),
            checksum_path=Path(cfg.provider_contract_checksum),
            provider_binary=Path(cfg.provider_binary),
        )
        cli = _executable(cfg.provider_schema_cli)
        schema_evidence = _verify_schema_evidence(toolchains=static.toolchains, cli=cli)
    except ProviderContractError:
        raise
    except (OSError, TypeError, ValueError) as exc:
        raise _fail("provider evidence could not be verified") from exc

    scope = tempfile.TemporaryDirectory(prefix="ubitofu-provider-")
    root = Path(scope.name)
    try:
        root.chmod(0o700)
        provider_dir, selected = _selected_provider(root, cfg.provider_binary)
        if _sha256(selected) != static.provider_sha256:
            raise _fail("provider binary mismatch")
        cli_config = root / "dev-override.tfrc"
        cli_config.write_text(
            "provider_installation {\n"
            "  dev_overrides {\n"
            f"    {json.dumps(_PROVIDER_ADDRESS)} = {json.dumps(str(provider_dir))}\n"
            "  }\n"
            "  direct {}\n"
            "}\n"
        )
        cli_config.chmod(0o600)
        environment = dict(os.environ)
        environment.update({
            "TF_CLI_CONFIG_FILE": str(cli_config),
            "TF_IN_AUTOMATION": "1",
            "CHECKPOINT_DISABLE": "1",
        })
        runner = TofuRunner(workdir=workdir, binary=str(cli), environment=environment)
        version = runner.version()
        schema = runner.providers_schema()
        if version != schema_evidence.version:
            raise _fail("schema CLI mismatch")
        if hashlib.sha256(_canonical_schema(schema, _PROVIDER_ADDRESS)).hexdigest() != (
            schema_evidence.canonical_schema_sha256
        ):
            raise _fail("provider schema mismatch")
        from .contract_diff import DEFAULT_DNS_CORPUS, require_dns_corpus_parity

        require_dns_corpus_parity(static.contract, DEFAULT_DNS_CORPUS, schema)
    except ProviderContractError:
        scope.cleanup()
        raise
    except (OSError, UbitofuError, ValueError) as exc:
        scope.cleanup()
        raise _fail("provider evidence could not be verified") from exc
    return ProviderExecution(
        contract=static.contract,
        schema=schema,
        binary=str(cli),
        environment=environment,
        _scope=scope,
    )


@contextmanager
def provider_execution(*, cfg: Config, workdir: Path) -> Iterator[ProviderExecution]:
    """Admit an optional contract and always clean its private files."""
    execution = _admit(cfg=cfg, workdir=workdir)
    try:
        yield execution
    finally:
        execution.cleanup()
