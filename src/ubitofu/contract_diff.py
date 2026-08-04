# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 James Braid
"""Versioned differential checks for the provider-contract shadow path."""

import copy
import hashlib
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, cast

from .cleaner import clean_resource, is_settable, strip_secret_shaped
from .enumerator import enumerate_controller
from .hcl_writer import render_json_fallback
from .import_emitter import emit_import_blocks
from .manifest import ResourceSpec, spec_for_type
from .pipeline import ExistenceDecision, ExistenceFacts, classify_existence
from .provider_contract import ContractError, ResolvedContract

DEFAULT_DNS_CORPUS = Path(__file__).with_name("dns_record_contract_v1.json")


@dataclass(frozen=True)
class DifferentialMismatch:
    case: str
    dimension: str
    expected_identity: str
    actual_identity: str
    expected: object
    actual: object


class _CorpusController:
    site = "default"

    def __init__(self, record: object) -> None:
        self.record = record

    def collection(self, _endpoint: str) -> list[dict[str, object]]:
        if self.record is None:
            return []
        if not isinstance(self.record, dict):
            raise ContractError("corpus record must be an object or null")
        return [self.record]


def _sha256_json(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _dns_schema(provider_schema: dict[str, Any]) -> dict[str, Any]:
    providers = provider_schema.get("provider_schemas")
    if not isinstance(providers, dict):
        raise ContractError("differential provider schema has no provider_schemas")
    for provider in providers.values():
        if not isinstance(provider, dict):
            continue
        resources = provider.get("resource_schemas", {})
        if isinstance(resources, dict) and isinstance(
            resources.get("unifi_dns_record"), dict
        ):
            return cast(dict[str, Any], resources["unifi_dns_record"])
    raise ContractError("differential provider schema has no unifi_dns_record")


def _plan_outcome(
    case: dict[str, Any],
    spec: ResourceSpec,
    import_identity: list[str],
    *,
    supported: bool,
    redacted_paths: list[str],
) -> str:
    facts = case.get("facts")
    if not isinstance(facts, dict):
        raise ContractError("corpus facts must be an object")
    required = ("config_present", "state_present", "live_changed")
    if any(not isinstance(facts.get(name), bool) for name in required):
        raise ContractError("corpus facts must contain boolean execution facts")
    if not supported:
        return "coverage-gap"
    if redacted_paths:
        return "redacted"
    if facts["live_changed"]:
        return "update"
    classification = classify_existence(
        ExistenceFacts(
            address=f"{spec.resource_type}.corpus",
            resource_type=spec.resource_type,
            config_present=facts["config_present"],
            state_present=facts["state_present"],
            live_identity=import_identity[0] if import_identity else None,
            live_present=case.get("record") is not None,
        )
    )
    if classification.kind is ExistenceDecision.IMPORT_EXISTING_CONFIG:
        return "import"
    return "no-op"


def _observation(
    case: dict[str, Any],
    spec: ResourceSpec,
    provider_schema: dict[str, Any],
    *,
    capture_eligible: bool,
    redact_schema_sensitive: bool,
    redact_secret_shaped: bool,
) -> dict[str, object]:
    result = enumerate_controller(
        _CorpusController(case.get("record")),  # type: ignore[arg-type]
        [spec],
    )
    attrs = copy.deepcopy(case.get("hcl_attributes", {}))
    if not isinstance(attrs, dict):
        raise ContractError("corpus hcl_attributes must be an object")
    resource_schema = _dns_schema(provider_schema)
    schema_attrs = resource_schema.get("block", {}).get("attributes", {})
    if not isinstance(schema_attrs, dict):
        raise ContractError("DNS provider schema attributes must be an object")
    supported = all(
        name in schema_attrs
        and isinstance(schema_attrs[name], dict)
        and is_settable(schema_attrs[name])
        for name in attrs
    )
    cleaned = clean_resource(attrs, resource_schema)
    redacted_paths = strip_secret_shaped(cleaned) if redact_secret_shaped else []
    slug = str(case.get("slug", case["name"])).replace("-", "_")
    generated_hcl = render_json_fallback(spec.resource_type, slug, cleaned)
    generated_hcl_sha256 = hashlib.sha256(generated_hcl.encode()).hexdigest()
    import_identity = [target.import_id for target in result.targets]
    plan_outcome = _plan_outcome(
        case,
        spec,
        import_identity,
        supported=supported,
        redacted_paths=redacted_paths,
    )
    coverage = [] if supported else ["unsupported DNS record"]
    receipt_inputs = {
        "resource": asdict(spec),
        "capture_eligible": capture_eligible,
        "schema_sensitive_redaction": redact_schema_sensitive,
        "secret_shaped_redaction": redact_secret_shaped,
        "plan_outcome": plan_outcome,
        "supported": supported,
        "generated_hcl_sha256": generated_hcl_sha256,
        "import_identity": import_identity,
    }
    return {
        "enumeration": {
            "targets": [asdict(target) for target in result.targets],
            "gaps": result.gaps,
        },
        "import_identity": import_identity,
        "import_hcl": emit_import_blocks(result.targets),
        "capture_eligibility": capture_eligible,
        "redaction": {
            "schema_sensitive": redact_schema_sensitive,
            "secret_shaped": redact_secret_shaped,
            "paths": redacted_paths,
        },
        "generated_hcl": generated_hcl,
        "generated_hcl_sha256": generated_hcl_sha256,
        "plan_outcome": plan_outcome,
        "coverage": coverage,
        "receipt_inputs": receipt_inputs,
        "receipt_inputs_sha256": _sha256_json(receipt_inputs),
    }


def _golden_mismatches(
    case: dict[str, Any], observation: dict[str, object]
) -> list[DifferentialMismatch]:
    expected = {
        "import_identity": case.get("expected_import_ids"),
        "redaction": case.get("expected_redacted_paths"),
        "generated_hcl": case.get("generated_hcl_sha256"),
        "plan_outcome": case.get("plan_outcome"),
        "coverage": [] if case.get("supported") is True else ["unsupported DNS record"],
        "receipt_inputs": case.get("receipt_inputs_sha256"),
    }
    actual = {
        "import_identity": observation["import_identity"],
        "redaction": observation["redaction"]["paths"],  # type: ignore[index]
        "generated_hcl": observation["generated_hcl_sha256"],
        "plan_outcome": observation["plan_outcome"],
        "coverage": observation["coverage"],
        "receipt_inputs": observation["receipt_inputs_sha256"],
    }
    return [
        DifferentialMismatch(
            case=case["name"],
            dimension=dimension,
            expected_identity="dns-corpus-v1/legacy-manifest",
            actual_identity="legacy-manifest",
            expected=expected_value,
            actual=actual[dimension],
        )
        for dimension, expected_value in expected.items()
        if actual[dimension] != expected_value
    ]


def compare_dns_corpus(
    contract: ResolvedContract,
    corpus_path: Path,
    provider_schema: dict[str, Any],
) -> list[DifferentialMismatch]:
    """Compare contract-mode DNS behavior with the authoritative manifest."""
    try:
        document = json.loads(corpus_path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ContractError(f"cannot read differential corpus {corpus_path}: {exc}") from exc
    if document.get("format_version") != 1:
        raise ContractError(
            "differential corpus format mismatch: "
            f"expected=1 actual={document.get('format_version')!r}"
        )
    resource_type = document.get("resource_type")
    if resource_type != "unifi_dns_record":
        raise ContractError(
            "differential corpus resource mismatch: "
            f"expected='unifi_dns_record' actual={resource_type!r}"
        )
    legacy = spec_for_type(resource_type)
    mismatches: list[DifferentialMismatch] = []
    for raw_case in document.get("cases", []):
        if not isinstance(raw_case, dict) or not isinstance(raw_case.get("name"), str):
            raise ContractError("each differential corpus case must have a name")
        case = raw_case
        expected = _observation(
            case,
            legacy,
            provider_schema,
            capture_eligible=True,
            redact_schema_sensitive=True,
            redact_secret_shaped=True,
        )
        mismatches.extend(_golden_mismatches(case, expected))
        actual = _observation(
            case,
            contract.resource_spec,
            provider_schema,
            capture_eligible=contract.capture_eligible,
            redact_schema_sensitive=contract.redact_schema_sensitive,
            redact_secret_shaped=contract.redact_secret_shaped,
        )
        for dimension, expected_value in expected.items():
            actual_value = actual[dimension]
            if actual_value != expected_value:
                mismatches.append(
                    DifferentialMismatch(
                        case=case["name"],
                        dimension=dimension,
                        expected_identity="legacy-manifest",
                        actual_identity=contract.contract_id,
                        expected=expected_value,
                        actual=actual_value,
                    )
                )
    return mismatches


def require_dns_corpus_parity(
    contract: ResolvedContract,
    corpus_path: Path,
    provider_schema: dict[str, Any],
) -> None:
    """Fail with both authorities named when a shadow observation diverges."""
    mismatches = compare_dns_corpus(contract, corpus_path, provider_schema)
    if not mismatches:
        return
    mismatch = mismatches[0]
    raise ContractError(
        "provider contract differential mismatch: "
        f"case={mismatch.case!r} dimension={mismatch.dimension!r} "
        f"expected_identity={mismatch.expected_identity!r} "
        f"expected={mismatch.expected!r} "
        f"actual_identity={mismatch.actual_identity!r} actual={mismatch.actual!r}"
    )
