# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 James Braid
"""Native provider-contract corpus regression coverage."""

from __future__ import annotations

import ast
import inspect
import json
from pathlib import Path

from ubitofu.manifest import spec_for_type
from ubitofu.provider_contract import ProviderContract


def _dns_schema() -> dict[str, object]:
    return {
        "format_version": "1.0",
        "provider_schemas": {
            "registry.terraform.io/ubiquiti-community/unifi": {
                "resource_schemas": {
                    "unifi_dns_record": {
                        "block": {
                            "attributes": {
                                "id": {"computed": True, "type": "string"},
                                "name": {"optional": True, "type": "string"},
                                "record_type": {"required": True, "type": "string"},
                                "ttl": {"optional": True, "type": "string"},
                                "value": {"required": True, "type": "string"},
                            }
                        }
                    }
                }
            }
        },
    }


def _contract() -> ProviderContract:
    return ProviderContract(
        contract_id="unifi_dns_record@native-v2",
        mode="provider_projection_required",
        resource_spec=spec_for_type("unifi_dns_record"),
        catalog_sha256="catalog-identity",
        lifecycle_receipt_sha256="receipt-identity",
        sidecar_sha256="sidecar-identity",
    )


def test_v2_corpus_uses_only_native_semantic_modules_and_matches_golden_cases() -> None:
    """Catches restoring the retired shadow evaluator or changing native behavior."""
    import ubitofu.contract_diff as contract_diff

    source = inspect.getsource(contract_diff)
    tree = ast.parse(source)
    imported = {
        node.module.split(".", 1)[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        and node.level
        if node.module is not None
    }
    assert not imported.intersection({"enumerator", "pipeline"})

    fixture = Path(__file__).parent / "fixtures" / "provider_contract" / "dns_record_v2.json"
    assert contract_diff.DEFAULT_DNS_CORPUS.read_bytes() == fixture.read_bytes()
    document = json.loads(fixture.read_text())
    assert document["format_version"] == 2
    assert {case["name"] for case in document["cases"]} == {
        "absent",
        "defaulted",
        "configured",
        "imported",
        "live-drifted",
        "sensitive",
        "unsupported",
        "no-op",
    }
    assert all(
        set(case["expected"]["plan_outcome"]) == {"changed", "blocked", "reason_codes"}
        for case in document["cases"]
    )

    mismatches = contract_diff.compare_dns_corpus(_contract(), fixture, _dns_schema())

    assert mismatches == []
    contract_diff.require_dns_corpus_parity(_contract(), fixture, _dns_schema())
