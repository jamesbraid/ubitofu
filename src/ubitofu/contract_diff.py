# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 James Braid
"""Versioned differential checks for the provider-contract shadow path."""

import copy
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .cleaner import strip_secret_shaped
from .enumerator import enumerate_controller
from .hcl_writer import render_json_fallback
from .import_emitter import emit_import_blocks
from .manifest import ResourceSpec, spec_for_type
from .provider_contract import ContractError, ResolvedContract


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


def _observation(
    case: dict[str, Any],
    spec: ResourceSpec,
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
    redacted_paths = strip_secret_shaped(attrs) if redact_secret_shaped else []
    slug = str(case.get("slug", case["name"])).replace("-", "_")
    generated_hcl = render_json_fallback(spec.resource_type, slug, attrs)
    coverage = [] if case.get("supported") is True else ["unsupported DNS record"]
    receipt_inputs = {
        "resource": asdict(spec),
        "capture_eligible": capture_eligible,
        "schema_sensitive_redaction": redact_schema_sensitive,
        "secret_shaped_redaction": redact_secret_shaped,
        "plan_outcome": case.get("plan_outcome"),
    }
    return {
        "enumeration": {
            "targets": [asdict(target) for target in result.targets],
            "gaps": result.gaps,
        },
        "import_identity": [target.import_id for target in result.targets],
        "import_hcl": emit_import_blocks(result.targets),
        "capture_eligibility": capture_eligible,
        "redaction": {
            "schema_sensitive": redact_schema_sensitive,
            "secret_shaped": redact_secret_shaped,
            "paths": redacted_paths,
        },
        "generated_hcl": generated_hcl,
        "plan_outcome": case.get("plan_outcome"),
        "coverage": coverage,
        "receipt_inputs": receipt_inputs,
    }


def compare_dns_corpus(
    contract: ResolvedContract, corpus_path: Path
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
            capture_eligible=True,
            redact_schema_sensitive=True,
            redact_secret_shaped=True,
        )
        actual = _observation(
            case,
            contract.resource_spec,
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


def require_dns_corpus_parity(contract: ResolvedContract, corpus_path: Path) -> None:
    """Fail with both authorities named when a shadow observation diverges."""
    mismatches = compare_dns_corpus(contract, corpus_path)
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
