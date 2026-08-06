from __future__ import annotations

import copy
import hashlib
import json
import os
from dataclasses import asdict
from pathlib import Path

import pytest

from ubitofu.catalog_contract import (
    CatalogContractError,
    CatalogContractEvidence,
    canonical_provider_projection,
    verify_catalog_contract,
    verify_catalog_contract_files,
)
from ubitofu.manifest import MANIFEST


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _surface_sets() -> dict[str, list[str]]:
    return {
        "managed_resource": sorted(spec.resource_type for spec in MANIFEST),
        "data_source": [f"unifi_data_{index:02d}" for index in range(13)],
        "list_resource": [f"unifi_list_{index:02d}" for index in range(25)],
        "action": ["unifi_port"],
    }


def _valid_contract() -> tuple[dict[str, object], CatalogContractEvidence]:
    digest = _digest("evidence")
    surfaces = _surface_sets()
    contract_surfaces = []
    schema_keys = {
        "managed_resource": "resource_schemas",
        "data_source": "data_source_schemas",
        "list_resource": "list_resource_schemas",
        "action": "action_schemas",
    }
    terraform_schema: dict[str, object] = {}
    tofu_schema: dict[str, object] = {}
    for kind, names in surfaces.items():
        key = schema_keys[kind]
        terraform_schema[key] = {name: {} for name in names}
        if kind in {"managed_resource", "data_source"}:
            tofu_schema[key] = {name: {} for name in names}
        for name in names:
            contract_surfaces.append(
                {
                    "kind": kind,
                    "name": name,
                    "state": "admitted",
                    "evidence_sha256": digest,
                    "capture_mode": (
                        "managed" if kind == "managed_resource" else "not_applicable"
                    ),
                }
            )
    contract = {
        "format_version": 1,
        "gate": "catalog-management-contract",
        "mode": "provider_catalog_projection_required",
        "result": "ready_for_downstream_verification",
        "provider_address": "registry.terraform.io/ubiquiti-community/unifi",
        "provider": {
            "source_commit": "a" * 40,
            "binary": {"sha256": digest},
            "schema": {
                "toolchains": {
                    "terraform": {
                        "version": "1.15.8",
                        "binary_sha256": _digest("terraform"),
                        "canonical_schema_sha256": _digest("terraform-schema"),
                    },
                    "tofu": {
                        "version": "1.12.1",
                        "binary_sha256": _digest("tofu"),
                        "canonical_schema_sha256": _digest("tofu-schema"),
                    },
                }
            },
        },
        "admission": {
            "receipt_sha256": _digest("admission"),
            "result": "pass",
            "admitted_surface_count": 67,
            "release_blockers": [
                {"kind": "action", "name": "unifi_port", "signal": "hardware_claim"}
            ],
        },
        "downstream": {
            "repository": "infra/ubitofu",
            "commit": "e9909d44e8084eba7e447c16ee8cfe646d6b3165",
            "manifest_path": "src/ubitofu/manifest.py",
            "manifest_sha256": _digest("manifest"),
        },
        "policy_sha256": _digest("policy"),
        "required_dimensions": [
            "capture_eligibility",
            "coverage",
            "enumeration",
            "generated_hcl",
            "identity",
            "plan_classification",
            "receipt_inputs",
            "redaction",
        ],
        "surfaces": contract_surfaces,
    }
    evidence = CatalogContractEvidence(
        contract_sha256=_digest("contract"),
        provider_binary_sha256=digest,
        downstream_commit=str(contract["downstream"]["commit"]),  # type: ignore[index]
        manifest_sha256=str(contract["downstream"]["manifest_sha256"]),  # type: ignore[index]
        toolchains={
            "terraform": {
                "version": "1.15.8",
                "binary_sha256": _digest("terraform"),
                "canonical_schema_sha256": _digest("terraform-schema"),
            },
            "tofu": {
                "version": "1.12.1",
                "binary_sha256": _digest("tofu"),
                "canonical_schema_sha256": _digest("tofu-schema"),
            },
        },
        terraform_schema=terraform_schema,
        tofu_schema=tofu_schema,
    )
    return contract, evidence


def test_catalog_contract_promotes_all_surfaces_and_retains_hardware_blocker() -> None:
    contract, evidence = _valid_contract()

    receipt = verify_catalog_contract(contract, evidence)

    assert receipt["result"] == "pass"
    assert receipt["surface_count"] == 67
    assert receipt["capture_counts"] == {"managed": 28, "not_applicable": 39}
    assert receipt["release_blockers"] == [
        {"kind": "action", "name": "unifi_port", "signal": "hardware_claim"}
    ]
    assert all(surface["state"] == "contract_parity" for surface in receipt["surfaces"])
    assert all(len(surface["receipt_sha256"]) == 64 for surface in receipt["surfaces"])
    managed = {
        surface["name"] for surface in receipt["surfaces"] if surface["capture_mode"] == "managed"
    }
    assert managed == {spec.resource_type for spec in MANIFEST}


@pytest.mark.parametrize(
    ("mutate_contract", "mutate_evidence", "match"),
    [
        (
            lambda contract: contract["surfaces"].pop(),
            lambda _evidence: None,
            "67 surfaces",
        ),
        (
            lambda contract: contract["surfaces"][0].update(state="adapter_parity"),
            lambda _evidence: None,
            "not admitted",
        ),
        (
            lambda contract: contract["surfaces"][0].update(capture_mode="not_applicable"),
            lambda _evidence: None,
            "managed resource set",
        ),
        (
            lambda _contract: None,
            lambda evidence: evidence.toolchains["terraform"].update(version="0.0.0"),
            "Terraform toolchain",
        ),
        (
            lambda _contract: None,
            lambda evidence: evidence.terraform_schema["resource_schemas"].pop(
                MANIFEST[0].resource_type
            ),
            "schema surface set",
        ),
        (
            lambda contract: contract["downstream"].update(manifest_sha256="b" * 64),
            lambda _evidence: None,
            "manifest SHA-256",
        ),
        (
            lambda contract: contract.update(unexpected="field"),
            lambda _evidence: None,
            "catalog contract fields",
        ),
    ],
)
def test_catalog_contract_fails_closed(
    mutate_contract: object,
    mutate_evidence: object,
    match: str,
) -> None:
    contract, evidence = _valid_contract()
    contract = copy.deepcopy(contract)
    mutate_contract(contract)  # type: ignore[operator]
    mutate_evidence(evidence)  # type: ignore[operator]

    with pytest.raises(CatalogContractError, match=match):
        verify_catalog_contract(contract, evidence)


def test_manifest_profiles_remain_value_free_and_serializable() -> None:
    profiles = [asdict(spec) for spec in MANIFEST]
    assert len(profiles) == 28
    assert all(profile["resource_type"].startswith("unifi_") for profile in profiles)


def _write_cli(path: Path, version: str) -> None:
    path.write_text(
        "#!/usr/bin/env python3\n"
        "import json\n"
        f"print(json.dumps({{'terraform_version': {version!r}}}))\n"
    )
    path.chmod(0o755)


def test_catalog_contract_files_measure_exact_local_evidence(tmp_path: Path) -> None:
    contract, _evidence = _valid_contract()
    provider = tmp_path / "terraform-provider-unifi"
    provider.write_bytes(b"candidate-provider")
    contract["provider"]["binary"]["sha256"] = hashlib.sha256(  # type: ignore[index]
        provider.read_bytes()
    ).hexdigest()

    root = Path(__file__).resolve().parents[1]
    manifest = root / "src/ubitofu/manifest.py"
    contract["downstream"]["manifest_sha256"] = hashlib.sha256(  # type: ignore[index]
        manifest.read_bytes()
    ).hexdigest()

    terraform = tmp_path / "terraform"
    tofu = tmp_path / "tofu"
    _write_cli(terraform, "1.15.8")
    _write_cli(tofu, "1.12.1")
    _surface_projection = _valid_contract()[1]
    schemas = {
        "terraform": _surface_projection.terraform_schema,
        "tofu": _surface_projection.tofu_schema,
    }
    schema_paths: dict[str, Path] = {}
    for name, cli in (("terraform", terraform), ("tofu", tofu)):
        projection = dict(schemas[name])
        projection["provider"] = {}
        raw = {"provider_schemas": {contract["provider_address"]: projection}}
        path = tmp_path / f"{name}-schema.json"
        path.write_text(json.dumps(raw))
        schema_paths[name] = path
        toolchain = contract["provider"]["schema"]["toolchains"][name]  # type: ignore[index]
        toolchain["binary_sha256"] = hashlib.sha256(cli.read_bytes()).hexdigest()
        toolchain["canonical_schema_sha256"] = hashlib.sha256(
            canonical_provider_projection(raw, str(contract["provider_address"]))[0]
        ).hexdigest()

    contract_path = tmp_path / "catalog-management-contract.json"
    contract_path.write_text(json.dumps(contract, sort_keys=True) + "\n")
    contract_sha256 = hashlib.sha256(contract_path.read_bytes()).hexdigest()
    output = tmp_path / "catalog-contract-parity.json"

    receipt = verify_catalog_contract_files(
        contract_path=contract_path,
        expected_contract_sha256=contract_sha256,
        provider_binary=provider,
        terraform_cli=terraform,
        terraform_schema=schema_paths["terraform"],
        tofu_cli=tofu,
        tofu_schema=schema_paths["tofu"],
        repository_root=root,
        output_path=output,
    )

    assert receipt["result"] == "pass"
    assert json.loads(output.read_text()) == receipt
    assert os.stat(output).st_mode & 0o777 == 0o600


def test_catalog_contract_files_reject_changed_manifest(tmp_path: Path) -> None:
    contract, _evidence = _valid_contract()
    contract_path = tmp_path / "contract.json"
    contract_path.write_text(json.dumps(contract))
    root = tmp_path / "repo"
    (root / "src/ubitofu").mkdir(parents=True)
    (root / "src/ubitofu/manifest.py").write_text("changed\n")

    with pytest.raises(CatalogContractError, match="manifest SHA-256"):
        verify_catalog_contract_files(
            contract_path=contract_path,
            expected_contract_sha256=hashlib.sha256(contract_path.read_bytes()).hexdigest(),
            provider_binary=tmp_path / "provider",
            terraform_cli=tmp_path / "terraform",
            terraform_schema=tmp_path / "terraform-schema.json",
            tofu_cli=tmp_path / "tofu",
            tofu_schema=tmp_path / "tofu-schema.json",
            repository_root=root,
        )


def test_catalog_contract_files_reject_unpinned_contract(tmp_path: Path) -> None:
    contract, _evidence = _valid_contract()
    contract_path = tmp_path / "contract.json"
    contract_path.write_text(json.dumps(contract))

    with pytest.raises(CatalogContractError, match="operator-pinned contract SHA-256"):
        verify_catalog_contract_files(
            contract_path=contract_path,
            expected_contract_sha256="0" * 64,
            provider_binary=tmp_path / "provider",
            terraform_cli=tmp_path / "terraform",
            terraform_schema=tmp_path / "terraform-schema.json",
            tofu_cli=tmp_path / "tofu",
            tofu_schema=tmp_path / "tofu-schema.json",
            repository_root=tmp_path,
        )
