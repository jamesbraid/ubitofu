# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 James Braid
import hashlib
import json
from pathlib import Path

import pytest

from ubitofu.config import Config
from ubitofu.provider_contract import (
    ProviderContractError,
    _canonical_schema,
    provider_execution,
)

ADDRESS = "registry.terraform.io/ubiquiti-community/unifi"


def _sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _projection(description: str = "selected-provider") -> dict[str, object]:
    return {
        "provider": {
            "version": 0,
            "block": {"attributes": {}},
            "description": description,
        },
        "resource_schemas": {
            "unifi_dns_record": {
                "version": 1,
                "block": {
                    "attributes": {
                        "name": {"type": "string", "optional": True},
                        "record_type": {"type": "string", "required": True},
                        "value": {"type": "string", "required": True},
                    }
                },
            }
        },
    }


def _fake_cli(
    tmp_path: Path, *, bad_schema: bool = False, marker: Path | None = None
) -> Path:
    cli = tmp_path / "terraform"
    schema_body = "raise SystemExit(12)" if bad_schema else """
config = pathlib.Path(os.environ["TF_CLI_CONFIG_FILE"]).read_text()
match = re.search(r'= ("(?:[^"\\\\]|\\\\.)*")', config)
provider_dir = pathlib.Path(json.loads(match.group(1)))
provider = next(provider_dir.glob("terraform-provider-unifi*"))
projection = json.loads((pathlib.Path.cwd() / "provider-projection.json").read_text())
projection["provider"]["description"] = provider.read_text()
print(json.dumps({
    "format_version": "1.0",
    "provider_schemas": {""" + repr(ADDRESS) + """: projection},
}))
"""
    cli.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, pathlib, re, sys\n"
        + (f"pathlib.Path({str(marker)!r}).write_text('ran')\n" if marker else "")
        +
        "if sys.argv[1:] == ['version', '-json']:\n"
        "    print(json.dumps({'terraform_version': '1.15.8'}))\n"
        "elif sys.argv[1:] == ['providers', 'schema', '-json']:\n"
        + "\n".join(f"    {line}" for line in schema_body.splitlines())
        + "\nelse:\n"
        "    raise SystemExit(99)\n"
    )
    cli.chmod(0o755)
    return cli


def _runtime_bundle(
    tmp_path: Path, *, bad_schema: bool = False, marker: Path | None = None
) -> tuple[Config, Path]:
    workdir = tmp_path / "work"
    workdir.mkdir()
    projection = _projection()
    (workdir / "provider-projection.json").write_text(json.dumps(projection))
    provider = tmp_path / "terraform-provider-unifi_v0.101.2"
    provider.write_text("selected-provider")
    provider.chmod(0o755)
    cli = _fake_cli(tmp_path, bad_schema=bad_schema, marker=marker)
    contract = tmp_path / "contract.json"
    contract.write_text(json.dumps({
        "format_version": 1,
        "contract_id": "unifi_dns_record@development-1",
        "mode": "provider_projection_required",
        "provider": {
            "address": ADDRESS,
            "binary": {"sha256": _sha(provider.read_bytes())},
            "schema": {"toolchains": {"terraform": {
                "version": "1.15.8",
                "binary_sha256": _sha(cli.read_bytes()),
                "canonical_schema_sha256": _sha(_canonical_schema(
                    {"provider_schemas": {ADDRESS: projection}}, ADDRESS
                )),
            }}},
        },
        "catalog": {"sha256": "catalog-identity"},
        "resource": {
            "resource_type": "unifi_dns_record",
            "endpoint": "v2/api/site/{site}/static-dns",
            "id_rule": "_id",
            "site_scoped": True,
            "capture_eligible": True,
            "redaction": {"schema_sensitive": True, "secret_shaped": True},
        },
        "lifecycle": {"receipt_sha256": "receipt-identity", "result": "pass"},
    }, sort_keys=True) + "\n")
    checksum = tmp_path / "contract.sha256"
    checksum.write_text(f"{_sha(contract.read_bytes())}  {contract.name}\n")
    return Config(
        controller_url="https://controller.example",
        site="default",
        workdir=str(workdir),
        provider_contract=str(contract),
        provider_contract_checksum=str(checksum),
        provider_binary=str(provider),
        provider_schema_cli=str(cli),
    ), provider


def test_provider_execution_admits_one_selected_provider_and_cleans_private_scope(tmp_path):
    cfg, provider = _runtime_bundle(tmp_path)

    with provider_execution(cfg=cfg, workdir=Path(cfg.workdir)) as execution:
        plan_path = Path(cfg.workdir) / "saved-plan.tfplan"
        runner = execution.runner(workdir=Path(cfg.workdir), plan_path=plan_path)
        config_path = Path(runner.environment["TF_CLI_CONFIG_FILE"])
        private_root = config_path.parent
        selected = next((private_root / "provider").glob("terraform-provider-unifi*"))

        assert selected.read_bytes() == provider.read_bytes()
        assert not selected.is_symlink()
        assert selected.stat().st_mode & 0o222 == 0
        assert private_root.stat().st_mode & 0o777 == 0o700
        assert runner.plan_path == plan_path
        description = runner.providers_schema()["provider_schemas"][ADDRESS]["provider"][
            "description"
        ]
        assert description == "selected-provider"

    assert not private_root.exists()


def test_provider_execution_rejects_bad_sidecars_without_exposing_their_content(tmp_path):
    cfg, _ = _runtime_bundle(tmp_path)
    Path(cfg.provider_contract_checksum).write_text("private-sidecar-value")

    with pytest.raises(ProviderContractError) as exc_info:
        with provider_execution(cfg=cfg, workdir=Path(cfg.workdir)):
            pass

    assert "sidecar checksum" in exc_info.value.reason
    assert "private-sidecar-value" not in str(exc_info.value)


def test_provider_execution_checks_sidecar_before_executing_the_cli(tmp_path):
    marker = tmp_path / "cli-ran"
    cfg, _ = _runtime_bundle(tmp_path, marker=marker)
    Path(cfg.provider_contract_checksum).write_text("not a checksum")

    with pytest.raises(ProviderContractError) as exc_info:
        with provider_execution(cfg=cfg, workdir=Path(cfg.workdir)):
            pass

    assert "sidecar checksum" in exc_info.value.reason
    assert not marker.exists()


def test_provider_execution_rejects_non_string_direct_bundle_values(tmp_path):
    cfg, _ = _runtime_bundle(tmp_path)
    cfg.provider_binary = 7  # type: ignore[assignment]

    with pytest.raises(ProviderContractError) as exc_info:
        with provider_execution(cfg=cfg, workdir=Path(cfg.workdir)):
            pass

    assert "non-empty strings" in exc_info.value.reason


def test_provider_execution_rejects_changed_schema_cli_identity(tmp_path):
    cfg, _ = _runtime_bundle(tmp_path)
    cli = Path(cfg.provider_schema_cli)
    cli.write_text(cli.read_text() + "# changed after contract capture\n")

    with pytest.raises(ProviderContractError) as exc_info:
        with provider_execution(cfg=cfg, workdir=Path(cfg.workdir)):
            pass

    assert "schema CLI mismatch" in exc_info.value.reason


def test_provider_execution_cleans_private_scope_when_schema_query_fails(monkeypatch, tmp_path):
    import ubitofu.provider_contract as provider_contract

    cfg, _ = _runtime_bundle(tmp_path, bad_schema=True)
    scopes = []
    real_scope = provider_contract.tempfile.TemporaryDirectory

    class RecordingScope:
        def __init__(self, *args, **kwargs):
            self._scope = real_scope(*args, **kwargs)
            self.name = self._scope.name
            self.cleaned = False
            scopes.append(self)

        def cleanup(self):
            self.cleaned = True
            self._scope.cleanup()

    monkeypatch.setattr(provider_contract.tempfile, "TemporaryDirectory", RecordingScope)

    with pytest.raises(ProviderContractError) as exc_info:
        with provider_execution(cfg=cfg, workdir=Path(cfg.workdir)):
            pass

    assert "provider evidence" in exc_info.value.reason
    assert len(scopes) == 1
    assert scopes[0].cleaned is True
    assert not Path(scopes[0].name).exists()


def test_provider_execution_returns_detached_cached_schema(tmp_path):
    cfg, _ = _runtime_bundle(tmp_path)

    with provider_execution(cfg=cfg, workdir=Path(cfg.workdir)) as execution:
        first = execution.runner(workdir=Path(cfg.workdir)).providers_schema()
        first["provider_schemas"][ADDRESS]["provider"]["description"] = "mutated"
        second = execution.runner(workdir=Path(cfg.workdir)).providers_schema()

    assert second["provider_schemas"][ADDRESS]["provider"]["description"] == "selected-provider"


def test_contract_failure_cannot_construct_a_controller(monkeypatch, tmp_path):
    import ubitofu.controller as controller

    cfg, _ = _runtime_bundle(tmp_path)
    Path(cfg.provider_contract_checksum).write_text("not a checksum")
    called = False

    def fail_if_called(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("controller construction must follow contract admission")

    monkeypatch.setattr(controller, "Controller", fail_if_called)

    with pytest.raises(ProviderContractError):
        with provider_execution(cfg=cfg, workdir=Path(cfg.workdir)):
            pass

    assert called is False


def test_canonical_schema_hashes_only_the_selected_provider_projection():
    schema = {
        "format_version": "1.0",
        "provider_schemas": {
            ADDRESS: {"provider": {"block": {"description": "x < y && café\u2028"}}},
            "synthetic/other": {"provider": {"block": {}}},
        },
    }

    assert _canonical_schema(schema, ADDRESS) == (
        b'{"provider":{"block":{"description":"x \\u003c y '
        b'\\u0026\\u0026 caf\xc3\xa9\\u2028"}}}\n'
    )
