# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 James Braid
"""Explicit, fail-closed resolution of provider management contracts."""

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import Config
from .manifest import ResourceSpec, spec_for_type


class ContractError(ValueError):
    """A contract bundle does not identify the installed provider evidence."""


@dataclass(frozen=True)
class ResolvedContract:
    contract_id: str
    mode: str
    resource_spec: ResourceSpec
    capture_eligible: bool
    redact_schema_sensitive: bool
    redact_secret_shaped: bool
    catalog_sha256: str
    lifecycle_receipt_sha256: str
    sidecar_sha256: str


def _sha256(path: Path, label: str) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as exc:
        raise ContractError(f"cannot hash {label} {path}: {exc}") from exc


def _mismatch(label: str, expected: object, actual: object) -> ContractError:
    return ContractError(f"{label}: expected={expected!r} actual={actual!r}")


def _load_checksum(path: Path, contract: Path) -> str:
    try:
        content = path.read_text().strip()
    except OSError as exc:
        raise ContractError(f"cannot read sidecar checksum {path}: {exc}") from exc
    parts = content.split()
    if len(parts) != 2:
        raise ContractError(
            f"sidecar checksum mismatch: expected='<sha256>  {contract.name}' "
            f"actual={content!r}"
        )
    digest, filename = parts
    if filename != contract.name:
        raise _mismatch("sidecar checksum filename mismatch", contract.name, filename)
    return digest


def _require_mapping(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ContractError(f"{label} must be an object")
    return value


def resolve_contract(
    *,
    contract: Path,
    checksum: Path,
    binary: Path,
    schema: Path,
    cli_name: str,
    cli_version: str,
    cli_sha256: str,
) -> ResolvedContract:
    """Resolve one sidecar against exact local provider and schema evidence."""
    expected_sidecar = _load_checksum(checksum, contract)
    actual_sidecar = _sha256(contract, "provider contract")
    if actual_sidecar != expected_sidecar:
        raise _mismatch("sidecar checksum mismatch", expected_sidecar, actual_sidecar)

    try:
        document = json.loads(contract.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ContractError(f"cannot read provider contract: {exc}") from exc
    root = _require_mapping(document, "provider contract")
    if root.get("format_version") != 1:
        raise _mismatch("contract format mismatch", 1, root.get("format_version"))
    if root.get("mode") != "provider_projection_required":
        raise _mismatch(
            "contract mode mismatch",
            "provider_projection_required",
            root.get("mode"),
        )
    contract_id = root.get("contract_id")
    if not isinstance(contract_id, str) or not contract_id:
        raise ContractError("contract_id is required")

    provider = _require_mapping(root.get("provider"), "provider")
    if provider.get("address") != "registry.terraform.io/ubiquiti-community/unifi":
        raise _mismatch(
            "provider address mismatch",
            "registry.terraform.io/ubiquiti-community/unifi",
            provider.get("address"),
        )
    binary_identity = _require_mapping(provider.get("binary"), "provider.binary")
    expected_binary = binary_identity.get("sha256")
    actual_binary = _sha256(binary, "provider binary")
    if actual_binary != expected_binary:
        raise _mismatch("provider binary mismatch", expected_binary, actual_binary)

    schema_identity = _require_mapping(provider.get("schema"), "provider.schema")
    toolchains = _require_mapping(schema_identity.get("toolchains"), "schema.toolchains")
    toolchain = _require_mapping(toolchains.get(cli_name), f"schema.toolchains.{cli_name}")
    expected_toolchain = (
        cli_name,
        toolchain.get("version"),
        toolchain.get("binary_sha256"),
    )
    actual_toolchain = (cli_name, cli_version, cli_sha256)
    if actual_toolchain != expected_toolchain:
        raise _mismatch("schema toolchain mismatch", expected_toolchain, actual_toolchain)
    expected_schema = toolchain.get("canonical_schema_sha256")
    actual_schema = _sha256(schema, "provider schema")
    if actual_schema != expected_schema:
        raise _mismatch("provider schema mismatch", expected_schema, actual_schema)

    catalog = _require_mapping(root.get("catalog"), "catalog")
    catalog_sha256 = catalog.get("sha256")
    if not isinstance(catalog_sha256, str) or not catalog_sha256:
        raise ContractError("catalog.sha256 is required")
    resource = _require_mapping(root.get("resource"), "resource")
    resource_spec = ResourceSpec(
        resource_type=str(resource.get("resource_type", "")),
        endpoint=str(resource.get("endpoint", "")),
        id_rule=str(resource.get("id_rule", "")),
        site_scoped=resource.get("site_scoped") is True,
    )
    try:
        legacy_spec = spec_for_type(resource_spec.resource_type)
    except KeyError as exc:
        raise ContractError(
            "legacy manifest mismatch: expected=known resource "
            f"actual={resource_spec.resource_type!r}"
        ) from exc
    if resource_spec != legacy_spec:
        raise _mismatch("legacy manifest mismatch", legacy_spec, resource_spec)
    if resource.get("capture_eligible") is not True:
        raise _mismatch("capture eligibility mismatch", True, resource.get("capture_eligible"))
    redaction = _require_mapping(resource.get("redaction"), "resource.redaction")
    if redaction.get("schema_sensitive") is not True:
        raise _mismatch(
            "schema-sensitive redaction mismatch",
            True,
            redaction.get("schema_sensitive"),
        )
    if redaction.get("secret_shaped") is not True:
        raise _mismatch(
            "secret-shaped redaction mismatch",
            True,
            redaction.get("secret_shaped"),
        )
    lifecycle = _require_mapping(root.get("lifecycle"), "lifecycle")
    lifecycle_receipt = lifecycle.get("receipt_sha256")
    if lifecycle.get("result") != "pass" or not isinstance(lifecycle_receipt, str) \
            or not lifecycle_receipt:
        raise ContractError("lifecycle identity must name a passing receipt")

    return ResolvedContract(
        contract_id=contract_id,
        mode=str(root["mode"]),
        resource_spec=resource_spec,
        capture_eligible=True,
        redact_schema_sensitive=True,
        redact_secret_shaped=True,
        catalog_sha256=catalog_sha256,
        lifecycle_receipt_sha256=lifecycle_receipt,
        sidecar_sha256=actual_sidecar,
    )


def resolve_configured_contract(cfg: Config) -> ResolvedContract | None:
    """Resolve the configured bundle, or retain the legacy manifest path."""
    values = {
        "provider_contract": cfg.provider_contract,
        "provider_contract_checksum": cfg.provider_contract_checksum,
        "provider_binary": cfg.provider_binary,
        "provider_schema": cfg.provider_schema,
        "provider_schema_cli": cfg.provider_schema_cli,
        "provider_schema_cli_version": cfg.provider_schema_cli_version,
        "provider_schema_cli_sha256": cfg.provider_schema_cli_sha256,
    }
    configured = {name for name, value in values.items() if value}
    if not configured:
        return None
    if len(configured) != len(values):
        missing = sorted(set(values) - configured)
        raise ContractError(
            "configured contract bundle is incomplete: " + ", ".join(missing)
        )
    return resolve_contract(
        contract=Path(cfg.provider_contract),
        checksum=Path(cfg.provider_contract_checksum),
        binary=Path(cfg.provider_binary),
        schema=Path(cfg.provider_schema),
        cli_name=cfg.provider_schema_cli,
        cli_version=cfg.provider_schema_cli_version,
        cli_sha256=cfg.provider_schema_cli_sha256,
    )
