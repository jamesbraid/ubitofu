# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 James Braid
import hashlib
import json
from dataclasses import replace
from pathlib import Path

import pytest

from ubitofu.config import Config
from ubitofu.contract_diff import compare_dns_corpus, require_dns_corpus_parity
from ubitofu.enumerator import enumerate_controller
from ubitofu.import_emitter import emit_import_blocks
from ubitofu.manifest import spec_for_type
from ubitofu.provider_contract import (
    ContractError,
    ContractExecution,
    _canonical_schema,
    resolve_configured_contract,
    resolve_configured_execution,
    resolve_contract,
)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _fake_cli(
    tmp_path: Path, *, version: str = "1.15.8", honor_override: bool = True
) -> Path:
    """Write a CLI that exposes the provider selected by its dev override."""
    cli = tmp_path / "terraform"
    provider_selection = (
        "    projection['provider']['description'] = provider.read_text().strip()\n"
        if honor_override
        else "    projection['provider']['description'] = 'provider-not-selected'\n"
    )
    cli.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, pathlib, re, sys\n"
        "if sys.argv[1:] == ['version', '-json']:\n"
        f"    print(json.dumps({{'terraform_version': {version!r}}}))\n"
        "elif sys.argv[1:] == ['providers', 'schema', '-json']:\n"
        "    config = pathlib.Path(os.environ['TF_CLI_CONFIG_FILE']).read_text()\n"
        "    match = re.search(r'dev_overrides \\{\\s*\"[^\"]+\" = (\"[^\"]+\")', config)\n"
        "    provider_dir = pathlib.Path(json.loads(match.group(1)))\n"
        "    provider = next(provider_dir.glob('terraform-provider-unifi*'))\n"
        "    projection_path = pathlib.Path.cwd() / 'provider-projection.json'\n"
        "    projection = json.loads(projection_path.read_text())\n"
        f"{provider_selection}"
        "    print(json.dumps({'format_version': '1.0', 'provider_schemas': "
        "{'registry.terraform.io/ubiquiti-community/unifi': projection}}))\n"
        "else:\n"
        "    raise SystemExit(91)\n"
    )
    cli.chmod(0o755)
    return cli


def _projection(description: str = "verified-provider") -> dict[str, object]:
    return {
        "provider": {"version": 0, "block": {"attributes": {}}, "description": description},
        "resource_schemas": {
            "unifi_dns_record": {
                "version": 1,
                "block": {"attributes": {"name": {"type": "string", "optional": True}}},
            }
        },
    }


def _canonical(projection: dict[str, object]) -> bytes:
    return (json.dumps(projection, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _runtime_bundle(
    tmp_path: Path,
    *,
    cli_version: str = "1.15.8",
    workdir_projection: dict[str, object] | None = None,
    honor_override: bool = True,
) -> tuple[Config, Path]:
    workdir = tmp_path / "work"
    workdir.mkdir()
    projection = workdir_projection or _projection()
    (workdir / "provider-projection.json").write_text(json.dumps(projection))
    cli = _fake_cli(tmp_path, version=cli_version, honor_override=honor_override)
    provider = tmp_path / "terraform-provider-unifi_v0.101.2"
    provider.write_text("verified-provider")
    provider.chmod(0o755)

    bundle = _bundle(tmp_path)
    document = json.loads(bundle["contract"].read_text())
    document["provider"]["binary"]["sha256"] = _sha(provider.read_bytes())
    document["provider"]["schema"]["toolchains"]["terraform"] = {
        "version": "1.15.8",
        "binary_sha256": _sha(cli.read_bytes()),
        "canonical_schema_sha256": _sha(_canonical(_projection())),
    }
    bundle["contract"].write_text(json.dumps(document, sort_keys=True) + "\n")
    bundle["checksum"].write_text(
        f"{_sha(bundle['contract'].read_bytes())}  {bundle['contract'].name}\n"
    )

    cfg = Config("https://controller.example", "default", workdir=str(workdir))
    cfg.provider_contract = str(bundle["contract"])
    cfg.provider_contract_checksum = str(bundle["checksum"])
    cfg.provider_binary = str(provider)
    cfg.provider_schema_cli = str(cli)
    return cfg, provider


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
                "toolchains": {
                    "terraform": {
                        "version": "1.15.8",
                        "binary_sha256": "terraform-sha256",
                        "canonical_schema_sha256": _sha(schema.read_bytes()),
                    },
                    "tofu": {
                        "version": "1.12.1",
                        "binary_sha256": "tofu-sha256",
                        "canonical_schema_sha256": _sha(schema.read_bytes()),
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

    tofu = resolve_contract(
        **bundle,
        cli_name="tofu",
        cli_version="1.12.1",
        cli_sha256="tofu-sha256",
    )
    assert tofu.sidecar_sha256 == resolved.sidecar_sha256


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

    cfg, _ = _runtime_bundle(tmp_path)
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


def test_contract_execution_uses_actual_cli_provider_and_workdir_schema(
    tmp_path: Path,
) -> None:
    cfg, provider = _runtime_bundle(tmp_path)

    execution = resolve_configured_execution(cfg)

    assert isinstance(execution, ContractExecution)
    assert execution.contract is not None
    assert execution.runner.workdir == Path(cfg.workdir)
    assert Path(execution.runner.binary).samefile(cfg.provider_schema_cli)
    cli_config = Path(execution.runner.environment["TF_CLI_CONFIG_FILE"])
    override_dir = Path(json.loads(cli_config.read_text().split(" = ", 1)[1].splitlines()[0]))
    selected = next(override_dir.glob("terraform-provider-unifi*"))
    assert selected.samefile(provider)
    assert execution.schema["provider_schemas"]


def test_schema_canonicalization_matches_go_json_bytes() -> None:
    address = "registry.terraform.io/ubiquiti-community/unifi"
    schema = {
        "format_version": "1.0",
        "provider_schemas": {
            address: {
                "provider": {
                    "version": 0,
                    "block": {"description": "x < y && café\u2028next"},
                }
            }
        },
    }

    assert _canonical_schema(schema, address) == (
        b'{"provider":{"block":{"description":"x \\u003c y '
        b'\\u0026\\u0026 caf\xc3\xa9\\u2028next"},"version":0}}\n'
    )


def test_caller_supplied_cli_identity_cannot_replace_actual_identity(tmp_path: Path) -> None:
    cfg, _ = _runtime_bundle(tmp_path, cli_version="1.15.9")
    cfg.provider_schema_cli_version = "1.15.8"
    document = json.loads(Path(cfg.provider_contract).read_text())
    cfg.provider_schema_cli_sha256 = document["provider"]["schema"]["toolchains"][
        "terraform"
    ]["binary_sha256"]

    with pytest.raises(ContractError, match="schema toolchain mismatch") as exc_info:
        resolve_configured_execution(cfg)

    assert "expected=" in str(exc_info.value)
    assert "actual=" in str(exc_info.value)
    assert "1.15.9" in str(exc_info.value)


def test_contract_schema_comes_from_real_command_workdir(tmp_path: Path) -> None:
    wrong_projection = _projection()
    dns_block = wrong_projection["resource_schemas"]["unifi_dns_record"]["block"]  # type: ignore[index]
    dns_block["attributes"]["ttl"] = {"type": "number", "optional": True}  # type: ignore[index]
    cfg, _ = _runtime_bundle(tmp_path, workdir_projection=wrong_projection)
    supplied = tmp_path / "claimed-schema.json"
    supplied.write_bytes(_canonical(_projection()))
    cfg.provider_schema = str(supplied)

    with pytest.raises(ContractError, match="provider schema mismatch") as exc_info:
        resolve_configured_execution(cfg)

    assert "expected=" in str(exc_info.value)
    assert "actual=" in str(exc_info.value)


def test_unused_provider_binary_cannot_pass_contract_mode(tmp_path: Path) -> None:
    cfg, _ = _runtime_bundle(tmp_path, honor_override=False)

    with pytest.raises(ContractError, match="provider schema mismatch") as exc_info:
        resolve_configured_execution(cfg)

    assert "expected=" in str(exc_info.value)
    assert "actual=" in str(exc_info.value)


def test_cli_dispatch_retains_the_verified_contract_runner(
    monkeypatch: pytest.MonkeyPatch, fixtures_dir: Path
) -> None:
    import ubitofu.cli as cli

    runner = object()
    execution = ContractExecution(contract=None, runner=runner)  # type: ignore[arg-type]
    seen: list[object] = []
    monkeypatch.setattr(cli, "resolve_configured_execution", lambda cfg: execution)

    def reconcile(cfg: Config, out: object, *, check: bool, execution: object) -> int:
        seen.append(execution)
        return 0

    monkeypatch.setattr(cli, "cmd_reconcile", reconcile)

    assert cli.main(["reconcile", "--config", str(fixtures_dir / "config.toml")]) == 0
    assert seen == [execution]


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


def test_versioned_dns_differential_corpus_covers_management_outcomes(
    tmp_path: Path, fixtures_dir: Path
) -> None:
    bundle = _bundle(tmp_path)
    resolved = resolve_contract(
        **bundle,
        cli_name="terraform",
        cli_version="1.15.8",
        cli_sha256="terraform-sha256",
    )
    corpus = fixtures_dir / "provider_contract" / "dns_record_v1.json"

    mismatches = compare_dns_corpus(resolved, corpus)

    assert mismatches == []
    names = {case["name"] for case in json.loads(corpus.read_text())["cases"]}
    assert names == {
        "absent",
        "defaulted",
        "configured",
        "imported",
        "live-drifted",
        "sensitive",
        "unsupported",
        "no-op",
    }


def test_differential_corpus_names_expected_and_actual_policy_identity(
    tmp_path: Path, fixtures_dir: Path
) -> None:
    bundle = _bundle(tmp_path)
    resolved = resolve_contract(
        **bundle,
        cli_name="terraform",
        cli_version="1.15.8",
        cli_sha256="terraform-sha256",
    )

    mismatches = compare_dns_corpus(
        replace(resolved, capture_eligible=False),
        fixtures_dir / "provider_contract" / "dns_record_v1.json",
    )

    assert mismatches
    assert mismatches[0].dimension == "capture_eligibility"
    assert mismatches[0].expected_identity == "legacy-manifest"
    assert mismatches[0].actual_identity == resolved.contract_id

    with pytest.raises(ContractError) as exc_info:
        require_dns_corpus_parity(
            replace(resolved, capture_eligible=False),
            fixtures_dir / "provider_contract" / "dns_record_v1.json",
        )
    diagnostic = str(exc_info.value)
    assert "expected_identity='legacy-manifest'" in diagnostic
    assert f"actual_identity='{resolved.contract_id}'" in diagnostic
    assert "capture_eligibility" in diagnostic
