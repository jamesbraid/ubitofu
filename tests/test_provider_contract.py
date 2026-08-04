# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 James Braid
import hashlib
import json
from pathlib import Path

import pytest

from ubitofu.config import Config
from ubitofu.enumerator import enumerate_controller
from ubitofu.import_emitter import emit_import_blocks
from ubitofu.manifest import spec_for_type
from ubitofu.provider_contract import (
    ContractError,
    resolve_configured_contract,
    resolve_contract,
)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _bundle(tmp_path: Path) -> dict[str, Path]:
    binary = tmp_path / "terraform-provider-unifi"
    schema = tmp_path / "provider-schema.json"
    binary.write_bytes(b"provider-binary")
    schema.write_bytes(b"canonical-provider-schema")
    document = {
        "format_version": 1,
        "contract_id": "unifi_dns_record@development-1",
        "mode": "provider_projection_required",
        "provider": {
            "address": "registry.terraform.io/ubiquiti-community/unifi",
            "version": "0.101.2",
            "binary": {
                "platform": "linux/amd64",
                "sha256": _sha(binary.read_bytes()),
            },
            "schema": {
                "canonical_sha256": _sha(schema.read_bytes()),
                "toolchains": {
                    "terraform": {
                        "version": "1.15.8",
                        "binary_sha256": "terraform-sha256",
                    },
                    "tofu": {
                        "version": "1.12.1",
                        "binary_sha256": "tofu-sha256",
                    },
                },
            },
        },
        "catalog": {
            "id": "unifi.network.dns_record@10.4.57",
            "sha256": "catalog-sha256",
        },
        "resource": {
            "resource_type": "unifi_dns_record",
            "endpoint": "v2/api/site/{site}/static-dns",
            "id_rule": "_id",
            "site_scoped": True,
            "capture_eligible": True,
            "redaction": {
                "schema_sensitive": True,
                "secret_shaped": True,
            },
        },
        "lifecycle": {
            "receipt_sha256": "lifecycle-sha256",
            "result": "pass",
        },
    }
    contract = tmp_path / "unifi_dns_record.contract.json"
    contract.write_text(json.dumps(document, sort_keys=True) + "\n")
    checksum = tmp_path / "unifi_dns_record.contract.sha256"
    checksum.write_text(f"{_sha(contract.read_bytes())}  {contract.name}\n")
    return {"contract": contract, "checksum": checksum, "binary": binary, "schema": schema}


def test_resolve_contract_matches_legacy_dns_manifest(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    resolved = resolve_contract(
        **bundle,
        cli_name="terraform",
        cli_version="1.15.8",
        cli_sha256="terraform-sha256",
    )

    assert resolved.mode == "provider_projection_required"
    assert resolved.resource_spec == spec_for_type("unifi_dns_record")
    assert resolved.capture_eligible is True
    assert resolved.catalog_sha256 == "catalog-sha256"


@pytest.mark.parametrize(
    ("mutation", "expected"),
    [
        ("sidecar", "sidecar checksum mismatch"),
        ("binary", "provider binary mismatch"),
        ("schema", "provider schema mismatch"),
        ("toolchain", "schema toolchain mismatch"),
    ],
)
def test_resolve_contract_fails_closed_on_identity_mismatch(
    tmp_path: Path, mutation: str, expected: str
) -> None:
    bundle = _bundle(tmp_path)
    cli_version = "1.15.8"
    if mutation == "sidecar":
        bundle["checksum"].write_text(f"{'0' * 64}  {bundle['contract'].name}\n")
    elif mutation == "binary":
        bundle["binary"].write_bytes(b"different-provider")
    elif mutation == "schema":
        bundle["schema"].write_bytes(b"different-schema")
    else:
        cli_version = "1.15.9"

    with pytest.raises(ContractError, match=expected) as exc_info:
        resolve_contract(
            **bundle,
            cli_name="terraform",
            cli_version=cli_version,
            cli_sha256="terraform-sha256",
        )
    assert "expected=" in str(exc_info.value)
    assert "actual=" in str(exc_info.value)


def test_resolve_contract_rejects_manifest_divergence(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    document = json.loads(bundle["contract"].read_text())
    document["resource"]["id_rule"] = "site:_id"
    bundle["contract"].write_text(json.dumps(document, sort_keys=True) + "\n")
    bundle["checksum"].write_text(
        f"{_sha(bundle['contract'].read_bytes())}  {bundle['contract'].name}\n"
    )

    with pytest.raises(ContractError, match="legacy manifest mismatch"):
        resolve_contract(
            **bundle,
            cli_name="terraform",
            cli_version="1.15.8",
            cli_sha256="terraform-sha256",
        )


def test_configured_contract_is_explicit_and_all_or_nothing(tmp_path: Path) -> None:
    cfg = Config("https://controller.example", "default")
    assert resolve_configured_contract(cfg) is None

    cfg.provider_contract = str(tmp_path / "missing.json")
    with pytest.raises(ContractError, match="configured contract bundle is incomplete"):
        resolve_configured_contract(cfg)

    bundle = _bundle(tmp_path)
    cfg.provider_contract = str(bundle["contract"])
    cfg.provider_contract_checksum = str(bundle["checksum"])
    cfg.provider_binary = str(bundle["binary"])
    cfg.provider_schema = str(bundle["schema"])
    cfg.provider_schema_cli = "terraform"
    cfg.provider_schema_cli_version = "1.15.8"
    cfg.provider_schema_cli_sha256 = "terraform-sha256"
    assert resolve_configured_contract(cfg) is not None


def test_resolve_contract_reports_missing_bundle_file(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    bundle["binary"].unlink()

    with pytest.raises(ContractError, match="cannot hash provider binary"):
        resolve_contract(
            **bundle,
            cli_name="terraform",
            cli_version="1.15.8",
            cli_sha256="terraform-sha256",
        )


def test_dns_contract_shadow_corpus_matches_legacy_manifest(
    tmp_path: Path, fixtures_dir: Path
) -> None:
    class CorpusController:
        site = "default"

        def __init__(self, record: object) -> None:
            self.record = record

        def collection(self, _endpoint: str) -> list[dict[str, object]]:
            return [] if self.record is None else [self.record]  # type: ignore[list-item]

    bundle = _bundle(tmp_path)
    resolved = resolve_contract(
        **bundle,
        cli_name="terraform",
        cli_version="1.15.8",
        cli_sha256="terraform-sha256",
    )
    corpus = json.loads(
        (fixtures_dir / "provider-contract/v1/dns-record.json").read_text()
    )
    assert corpus["format_version"] == 1
    assert {case["name"] for case in corpus["cases"]} == {
        "absent",
        "defaulted",
        "configured",
        "imported",
        "live-drifted",
        "sensitive-shaped",
        "unsupported",
    }

    legacy_spec = spec_for_type(corpus["resource_type"])
    for case in corpus["cases"]:
        ctl = CorpusController(case["record"])
        legacy = enumerate_controller(ctl, [legacy_spec])  # type: ignore[arg-type]
        contract = enumerate_controller(
            ctl, [resolved.resource_spec]  # type: ignore[arg-type]
        )
        assert contract == legacy, case["name"]
        assert emit_import_blocks(contract.targets) == emit_import_blocks(legacy.targets)
        assert case["plan_outcome"] in {
            "no-op", "import", "update", "redacted", "coverage-gap"
        }

    assert resolved.capture_eligible is True
    assert resolved.redact_schema_sensitive is True
    assert resolved.redact_secret_shaped is True
    assert resolved.lifecycle_receipt_sha256 == "lifecycle-sha256"
