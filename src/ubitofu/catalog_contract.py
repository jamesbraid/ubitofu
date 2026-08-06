# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 James Braid
"""Fail-closed verification for the provider full-catalog management contract."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .manifest import MANIFEST


class CatalogContractError(ValueError):
    """The provider catalog contract does not match measured downstream evidence."""


@dataclass
class CatalogContractEvidence:
    contract_sha256: str
    provider_binary_sha256: str
    downstream_commit: str
    manifest_sha256: str
    toolchains: dict[str, dict[str, str]]
    terraform_schema: dict[str, object]
    tofu_schema: dict[str, object]


_PROVIDER_ADDRESS = "registry.terraform.io/ubiquiti-community/unifi"
_DOWNSTREAM_REPOSITORY = "ubitofu"
_DOWNSTREAM_MANIFEST = "src/ubitofu/manifest.py"
_DIMENSIONS = [
    "capture_eligibility",
    "coverage",
    "enumeration",
    "generated_hcl",
    "identity",
    "plan_classification",
    "receipt_inputs",
    "redaction",
]
_SCHEMA_KEYS = {
    "managed_resource": "resource_schemas",
    "data_source": "data_source_schemas",
    "list_resource": "list_resource_schemas",
    "action": "action_schemas",
}


def _object(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise CatalogContractError(f"{label} must be an object")
    return value


def _hex(value: object, length: int, label: str) -> str:
    if not isinstance(value, str) or len(value) != length:
        raise CatalogContractError(f"{label} must be a {length}-character hexadecimal digest")
    try:
        int(value, 16)
    except ValueError as exc:
        raise CatalogContractError(f"{label} must be hexadecimal") from exc
    return value


def _equal(actual: object, expected: object, label: str) -> None:
    if actual != expected:
        raise CatalogContractError(f"{label} mismatch: expected={expected!r} actual={actual!r}")


def _require_keys(value: dict[str, Any], expected: set[str], label: str) -> None:
    actual = set(value)
    if actual != expected:
        raise CatalogContractError(
            f"{label} fields mismatch: expected={sorted(expected)!r} actual={sorted(actual)!r}"
        )


def _schema_names(schema: dict[str, object], key: str, label: str) -> set[str]:
    value = _object(schema.get(key), f"{label} {key}")
    return set(value)


def _canonical_sha256(value: object) -> str:
    data = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    return hashlib.sha256(data).hexdigest()


def canonical_provider_projection(
    schema: dict[str, object], provider_address: str = _PROVIDER_ADDRESS
) -> tuple[bytes, dict[str, object]]:
    """Return the Go-compatible canonical provider projection and parsed value."""

    providers = _object(schema.get("provider_schemas"), "provider_schemas")
    projection = _object(
        providers.get(provider_address), f"provider_schemas.{provider_address}"
    )
    _object(projection.get("provider"), f"provider_schemas.{provider_address}.provider")
    try:
        encoded = json.dumps(
            projection,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise CatalogContractError(f"provider schema cannot be canonicalized: {exc}") from exc
    encoded = (
        encoded.replace("&", "\\u0026")
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )
    return (encoded + "\n").encode(), projection


def _sha256_file(path: Path, label: str) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as exc:
        raise CatalogContractError(f"cannot hash {label} {path}: {exc}") from exc


def _load_json(path: Path, label: str) -> tuple[dict[str, object], bytes]:
    def reject_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
        value: dict[str, object] = {}
        for key, item in pairs:
            if key in value:
                raise CatalogContractError(f"{label} contains duplicate field {key!r}")
            value[key] = item
        return value

    try:
        data = path.read_bytes()
        value = json.loads(data, object_pairs_hook=reject_duplicates)
    except CatalogContractError:
        raise
    except (OSError, json.JSONDecodeError) as exc:
        raise CatalogContractError(f"cannot read {label} {path}: {exc}") from exc
    return _object(value, label), data


def _resolve_cli(binary: Path, expected_name: str) -> Path:
    candidate = shutil.which(str(binary))
    path = Path(candidate) if candidate is not None else binary
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise CatalogContractError(f"cannot resolve {expected_name} CLI {binary}: {exc}") from exc
    name = resolved.name.lower()
    accepted = ("terraform",) if expected_name == "terraform" else ("tofu", "opentofu")
    if not name.startswith(accepted):
        raise CatalogContractError(
            f"{expected_name} CLI has unexpected executable name {resolved.name!r}"
        )
    return resolved


def _measure_toolchain(
    *, name: str, binary: Path, schema_path: Path
) -> tuple[dict[str, str], dict[str, object]]:
    cli = _resolve_cli(binary, name)
    environment = dict(os.environ)
    environment.update(
        {
            "CHECKPOINT_DISABLE": "1",
            "GIT_TERMINAL_PROMPT": "0",
            "TF_IN_AUTOMATION": "1",
        }
    )
    try:
        proc = subprocess.run(
            [str(cli), "version", "-json"],
            check=True,
            capture_output=True,
            text=True,
            timeout=15,
            env=environment,
        )
        version_document = json.loads(proc.stdout)
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError) as exc:
        raise CatalogContractError(f"cannot measure {name} CLI {cli}: {exc}") from exc
    version_object = _object(version_document, f"{name} version output")
    version = version_object.get("terraform_version")
    if not isinstance(version, str) or not version:
        raise CatalogContractError(f"{name} version output lacks terraform_version")
    raw_schema, _data = _load_json(schema_path, f"{name} provider schema")
    canonical, projection = canonical_provider_projection(raw_schema)
    return (
        {
            "version": version,
            "binary_sha256": _sha256_file(cli, f"{name} CLI"),
            "canonical_schema_sha256": hashlib.sha256(canonical).hexdigest(),
        },
        projection,
    )


def _verify_manifest_object(
    contract: dict[str, object], repository_root: Path
) -> tuple[str, str]:
    downstream = _object(contract.get("downstream"), "downstream")
    commit = _hex(downstream.get("commit"), 40, "downstream commit")
    manifest_path = downstream.get("manifest_path")
    if manifest_path != _DOWNSTREAM_MANIFEST:
        raise CatalogContractError(
            f"downstream manifest path mismatch: expected={_DOWNSTREAM_MANIFEST!r} "
            f"actual={manifest_path!r}"
        )
    expected = _hex(downstream.get("manifest_sha256"), 64, "downstream manifest SHA-256")
    working_manifest = repository_root / _DOWNSTREAM_MANIFEST
    actual = _sha256_file(working_manifest, "downstream manifest")
    _equal(actual, expected, "manifest SHA-256")

    environment = dict(os.environ)
    environment["GIT_TERMINAL_PROMPT"] = "0"
    try:
        resolved = subprocess.run(
            [
                "git",
                "--no-optional-locks",
                "-C",
                str(repository_root),
                "rev-parse",
                "--verify",
                f"{commit}^{{commit}}",
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=15,
            env=environment,
        ).stdout.strip()
        blob = subprocess.run(
            [
                "git",
                "--no-optional-locks",
                "-C",
                str(repository_root),
                "show",
                f"{commit}:{_DOWNSTREAM_MANIFEST}",
            ],
            check=True,
            capture_output=True,
            timeout=15,
            env=environment,
        ).stdout
    except (OSError, subprocess.SubprocessError) as exc:
        raise CatalogContractError(f"cannot resolve pinned downstream manifest: {exc}") from exc
    _equal(resolved, commit, "downstream commit")
    _equal(hashlib.sha256(blob).hexdigest(), expected, "pinned manifest SHA-256")
    return commit, expected


def _write_receipt(path: Path, receipt: dict[str, object]) -> None:
    data = (json.dumps(receipt, sort_keys=True, separators=(",", ":")) + "\n").encode()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_name = ""
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb", prefix=".catalog-contract-parity-", dir=path.parent, delete=False
        ) as temporary:
            temporary_name = temporary.name
            temporary.write(data)
            temporary.flush()
            os.fsync(temporary.fileno())
            os.fchmod(temporary.fileno(), 0o600)
        os.replace(temporary_name, path)
    except OSError as exc:
        raise CatalogContractError(f"cannot write catalog contract receipt {path}: {exc}") from exc
    finally:
        if temporary_name:
            try:
                Path(temporary_name).unlink(missing_ok=True)
            except OSError:
                pass


def _validate_identity(contract: dict[str, object], evidence: CatalogContractEvidence) -> None:
    _require_keys(
        contract,
        {
            "format_version",
            "gate",
            "mode",
            "result",
            "provider_address",
            "provider",
            "admission",
            "downstream",
            "policy_sha256",
            "required_dimensions",
            "surfaces",
        },
        "catalog contract",
    )
    _equal(contract.get("format_version"), 1, "catalog contract format")
    _equal(contract.get("gate"), "catalog-management-contract", "catalog contract gate")
    _equal(
        contract.get("mode"),
        "provider_catalog_projection_required",
        "catalog contract mode",
    )
    _equal(
        contract.get("result"),
        "ready_for_downstream_verification",
        "catalog contract result",
    )
    _equal(contract.get("provider_address"), _PROVIDER_ADDRESS, "provider address")
    _hex(evidence.contract_sha256, 64, "catalog contract SHA-256")

    provider = _object(contract.get("provider"), "provider")
    _require_keys(provider, {"source_commit", "binary", "schema"}, "provider")
    _hex(provider.get("source_commit"), 40, "provider source commit")
    binary = _object(provider.get("binary"), "provider binary")
    _require_keys(binary, {"sha256"}, "provider binary")
    expected_binary = _hex(binary.get("sha256"), 64, "provider binary SHA-256")
    _equal(evidence.provider_binary_sha256, expected_binary, "provider binary SHA-256")

    schema = _object(provider.get("schema"), "provider schema")
    _require_keys(schema, {"toolchains"}, "provider schema")
    expected_toolchains = _object(schema.get("toolchains"), "provider schema toolchains")
    _equal(set(expected_toolchains), {"terraform", "tofu"}, "schema toolchain set")
    _equal(set(evidence.toolchains), {"terraform", "tofu"}, "measured toolchain set")
    for name, display in (("terraform", "Terraform"), ("tofu", "OpenTofu")):
        expected = _object(expected_toolchains[name], f"{display} toolchain")
        _require_keys(
            expected,
            {"version", "binary_sha256", "canonical_schema_sha256"},
            f"{display} toolchain",
        )
        actual = _object(evidence.toolchains[name], f"measured {display} toolchain")
        _equal(actual, expected, f"{display} toolchain")
        if not isinstance(expected.get("version"), str) or not expected["version"]:
            raise CatalogContractError(f"{display} toolchain version is required")
        _hex(expected.get("binary_sha256"), 64, f"{display} binary SHA-256")
        _hex(
            expected.get("canonical_schema_sha256"),
            64,
            f"{display} canonical schema SHA-256",
        )


def _validate_downstream(contract: dict[str, object], evidence: CatalogContractEvidence) -> None:
    downstream = _object(contract.get("downstream"), "downstream")
    _require_keys(
        downstream,
        {"repository", "commit", "manifest_path", "manifest_sha256"},
        "downstream",
    )
    _equal(downstream.get("repository"), _DOWNSTREAM_REPOSITORY, "downstream repository")
    _equal(downstream.get("manifest_path"), _DOWNSTREAM_MANIFEST, "downstream manifest path")
    expected_commit = _hex(downstream.get("commit"), 40, "downstream commit")
    expected_manifest = _hex(
        downstream.get("manifest_sha256"), 64, "downstream manifest SHA-256"
    )
    _equal(evidence.downstream_commit, expected_commit, "downstream commit")
    _equal(evidence.manifest_sha256, expected_manifest, "manifest SHA-256")
    _hex(contract.get("policy_sha256"), 64, "management policy SHA-256")
    _equal(contract.get("required_dimensions"), _DIMENSIONS, "required dimensions")


def _validate_admission(contract: dict[str, object]) -> list[dict[str, Any]]:
    admission = _object(contract.get("admission"), "catalog admission")
    _require_keys(
        admission,
        {"receipt_sha256", "result", "admitted_surface_count", "release_blockers"},
        "catalog admission",
    )
    _hex(admission.get("receipt_sha256"), 64, "catalog admission receipt SHA-256")
    _equal(admission.get("result"), "pass", "catalog admission result")
    _equal(admission.get("admitted_surface_count"), 67, "catalog admission surface count")
    blockers = admission.get("release_blockers")
    expected = [{"kind": "action", "name": "unifi_port", "signal": "hardware_claim"}]
    _equal(blockers, expected, "catalog release blockers")
    return expected


def _validate_surfaces(
    contract: dict[str, object],
    evidence: CatalogContractEvidence,
) -> list[dict[str, Any]]:
    raw_surfaces = contract.get("surfaces")
    if not isinstance(raw_surfaces, list) or len(raw_surfaces) != 67:
        count = len(raw_surfaces) if isinstance(raw_surfaces, list) else 0
        raise CatalogContractError(f"catalog contract has {count} surfaces, want 67 surfaces")

    surfaces: list[dict[str, Any]] = []
    names_by_kind: dict[str, set[str]] = {kind: set() for kind in _SCHEMA_KEYS}
    seen: set[tuple[str, str]] = set()
    for index, raw_surface in enumerate(raw_surfaces):
        surface = _object(raw_surface, f"surface {index}")
        _require_keys(
            surface,
            {"kind", "name", "state", "evidence_sha256", "capture_mode"},
            f"surface {index}",
        )
        kind = surface.get("kind")
        name = surface.get("name")
        if (
            not isinstance(kind, str)
            or kind not in _SCHEMA_KEYS
            or not isinstance(name, str)
            or not name.startswith("unifi_")
        ):
            raise CatalogContractError(f"surface {index} has an invalid kind or name")
        surface_key = (kind, name)
        if surface_key in seen:
            raise CatalogContractError(f"surface {kind}/{name} is duplicated")
        seen.add(surface_key)
        names_by_kind[kind].add(name)
        if surface.get("state") != "admitted":
            raise CatalogContractError(f"surface {kind}/{name} is not admitted")
        _hex(surface.get("evidence_sha256"), 64, f"surface {kind}/{name} evidence SHA-256")
        surfaces.append(surface)

    managed_manifest = {spec.resource_type for spec in MANIFEST}
    if len(MANIFEST) != 28 or len(managed_manifest) != 28:
        raise CatalogContractError("downstream MANIFEST must contain 28 unique managed resources")
    managed_contract = {
        surface["name"]
        for surface in surfaces
        if surface.get("capture_mode") == "managed"
    }
    if (
        managed_contract != managed_manifest
        or names_by_kind["managed_resource"] != managed_manifest
    ):
        raise CatalogContractError("managed resource set differs from the downstream MANIFEST")
    for surface in surfaces:
        expected_mode = "managed" if surface["kind"] == "managed_resource" else "not_applicable"
        if surface.get("capture_mode") != expected_mode:
            raise CatalogContractError(
                f"surface {surface['kind']}/{surface['name']} has invalid capture mode"
            )

    for kind, schema_key in _SCHEMA_KEYS.items():
        actual = _schema_names(evidence.terraform_schema, schema_key, "Terraform schema")
        if actual != names_by_kind[kind]:
            raise CatalogContractError(f"Terraform schema surface set differs for {kind}")
    for kind in ("managed_resource", "data_source"):
        schema_key = _SCHEMA_KEYS[kind]
        actual = _schema_names(evidence.tofu_schema, schema_key, "OpenTofu schema")
        if actual != names_by_kind[kind]:
            raise CatalogContractError(f"OpenTofu schema surface set differs for {kind}")
    return surfaces


def verify_catalog_contract(
    contract: dict[str, object],
    evidence: CatalogContractEvidence,
) -> dict[str, object]:
    """Bind a provider catalog contract to measured ubitofu and schema evidence."""

    _validate_identity(contract, evidence)
    _validate_downstream(contract, evidence)
    blockers = _validate_admission(contract)
    surfaces = _validate_surfaces(contract, evidence)

    manifest_by_name = {spec.resource_type: asdict(spec) for spec in MANIFEST}
    receipts: list[dict[str, object]] = []
    counts = {"managed": 0, "not_applicable": 0}
    for surface in sorted(surfaces, key=lambda item: (str(item["kind"]), str(item["name"]))):
        mode = str(surface["capture_mode"])
        counts[mode] += 1
        binding = {
            "contract_sha256": evidence.contract_sha256,
            "surface": {"kind": surface["kind"], "name": surface["name"]},
            "capture_mode": mode,
            "manifest_spec": manifest_by_name.get(str(surface["name"])),
            "dimensions": _DIMENSIONS,
        }
        receipts.append(
            {
                "kind": surface["kind"],
                "name": surface["name"],
                "state": "contract_parity",
                "capture_mode": mode,
                "receipt_sha256": _canonical_sha256(binding),
            }
        )

    return {
        "format_version": 1,
        "gate": "ubitofu-catalog-contract-parity",
        "result": "pass",
        "contract_sha256": evidence.contract_sha256,
        "provider_binary_sha256": evidence.provider_binary_sha256,
        "downstream": {
            "repository": _DOWNSTREAM_REPOSITORY,
            "commit": evidence.downstream_commit,
            "manifest_path": _DOWNSTREAM_MANIFEST,
            "manifest_sha256": evidence.manifest_sha256,
        },
        "surface_count": len(receipts),
        "capture_counts": counts,
        "dimensions": {dimension: True for dimension in _DIMENSIONS},
        "release_blockers": blockers,
        "surfaces": receipts,
    }


def verify_catalog_contract_files(
    *,
    contract_path: Path,
    expected_contract_sha256: str,
    provider_binary: Path,
    terraform_cli: Path,
    terraform_schema: Path,
    tofu_cli: Path,
    tofu_schema: Path,
    repository_root: Path,
    output_path: Path | None = None,
) -> dict[str, object]:
    """Measure local artifacts and verify one provider catalog contract."""

    contract, contract_data = _load_json(contract_path, "catalog management contract")
    actual_contract_sha256 = hashlib.sha256(contract_data).hexdigest()
    _equal(
        actual_contract_sha256,
        _hex(expected_contract_sha256, 64, "operator-pinned contract SHA-256"),
        "operator-pinned contract SHA-256",
    )
    commit, manifest_sha256 = _verify_manifest_object(contract, repository_root)
    terraform_toolchain, terraform_projection = _measure_toolchain(
        name="terraform", binary=terraform_cli, schema_path=terraform_schema
    )
    tofu_toolchain, tofu_projection = _measure_toolchain(
        name="tofu", binary=tofu_cli, schema_path=tofu_schema
    )
    evidence = CatalogContractEvidence(
        contract_sha256=actual_contract_sha256,
        provider_binary_sha256=_sha256_file(provider_binary, "provider binary"),
        downstream_commit=commit,
        manifest_sha256=manifest_sha256,
        toolchains={"terraform": terraform_toolchain, "tofu": tofu_toolchain},
        terraform_schema=terraform_projection,
        tofu_schema=tofu_projection,
    )
    receipt = verify_catalog_contract(contract, evidence)
    if output_path is not None:
        _write_receipt(output_path, receipt)
    return receipt


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m ubitofu.catalog_contract",
        description="Verify the provider full-catalog management contract against local evidence.",
    )
    parser.add_argument("--contract", type=Path, required=True)
    parser.add_argument("--contract-sha256", required=True)
    parser.add_argument("--provider-binary", type=Path, required=True)
    parser.add_argument("--terraform", type=Path, required=True)
    parser.add_argument("--terraform-schema", type=Path, required=True)
    parser.add_argument("--tofu", type=Path, required=True)
    parser.add_argument("--tofu-schema", type=Path, required=True)
    parser.add_argument("--repository-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        verify_catalog_contract_files(
            contract_path=args.contract,
            expected_contract_sha256=args.contract_sha256,
            provider_binary=args.provider_binary,
            terraform_cli=args.terraform,
            terraform_schema=args.terraform_schema,
            tofu_cli=args.tofu,
            tofu_schema=args.tofu_schema,
            repository_root=args.repository_root,
            output_path=args.output,
        )
    except CatalogContractError as exc:
        print(f"ubitofu catalog contract: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
