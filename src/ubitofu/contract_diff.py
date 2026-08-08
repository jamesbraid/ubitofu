# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 James Braid
"""Native differential checks for the admitted DNS provider contract."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from typing import Any

from .cleaner import clean_resource, strip_secret_shaped
from .controller_projection import build_controller_snapshot, project_controller_snapshot
from .coverage import CoverageReport
from .generate import (
    GeneratedResource,
    GeneratePreview,
    GenerateSnapshot,
    generate_outcome,
    render_generate,
)
from .module_index import ModuleIndex, reindex_module
from .outcomes import reconcile_outcome
from .provider_contract import ProviderContract
from .reconcile_model import (
    ActionVector,
    ControllerSnapshot,
    FileIdentity,
    OpenTofuAddress,
    PlanDocument,
    ProviderSchema,
    ReconcilePlan,
    ReconcileSnapshot,
    ResourceChange,
    SourceAttribute,
    SourceResource,
    StateDocument,
    parse_opentofu_address,
)
from .reconcile_planner import build_reconcile_plan
from .reconcile_renderer import render_reconcile
from .reconcile_snapshot import normalize_reconcile_snapshot
from .tofu_json import parse_provider_schema
from .values import FrozenObject, freeze_value

DEFAULT_DNS_CORPUS = Path(__file__).with_name("dns_record_contract_v2.json")


@dataclass(frozen=True)
class DifferentialMismatch:
    case: str
    dimension: str
    expected: object
    actual: object


def compare_dns_corpus(
    contract: ProviderContract,
    corpus_path: Path,
    provider_schema: dict[str, Any],
) -> list[DifferentialMismatch]:
    """Evaluate the native v2 corpus through the released semantic seams."""
    document = _read_corpus(corpus_path)
    if document.get("format_version") != 2:
        raise ValueError("provider contract corpus format is unsupported")
    if document.get("resource_type") != "unifi_dns_record":
        raise ValueError("provider contract corpus resource is unsupported")
    if contract.resource_spec.resource_type != document["resource_type"]:
        raise ValueError("provider contract corpus resource does not match contract")
    cases = document.get("cases")
    if not isinstance(cases, list):
        raise ValueError("provider contract corpus cases are invalid")

    schema = parse_provider_schema(provider_schema)
    resource_schema = _resource_schema(provider_schema)
    mismatches: list[DifferentialMismatch] = []
    for raw_case in cases:
        case = _mapping(raw_case, "case")
        name = _string(case.get("name"), "case name")
        expected = _mapping(case.get("expected"), "case expected values")
        actual = _evaluate_case(case, schema=schema, resource_schema=resource_schema)
        for dimension in (
            "supported",
            "plan_outcome",
            "import_ids",
            "redacted_paths",
            "generated_hcl_sha256",
            "receipt_input_digests",
        ):
            if expected.get(dimension) != actual[dimension]:
                mismatches.append(
                    DifferentialMismatch(
                        name,
                        dimension,
                        expected.get(dimension),
                        actual[dimension],
                    )
                )
    return mismatches


def require_dns_corpus_parity(
    contract: ProviderContract,
    corpus_path: Path,
    provider_schema: dict[str, Any],
) -> None:
    """Reject a contract when its native behavioral corpus no longer matches."""
    mismatches = compare_dns_corpus(contract, corpus_path, provider_schema)
    if not mismatches:
        return
    mismatch = mismatches[0]
    raise ValueError(
        "provider contract differential mismatch: "
        f"case={mismatch.case!r} dimension={mismatch.dimension!r}"
    )


def _evaluate_case(
    case: dict[str, object],
    *,
    schema: ProviderSchema,
    resource_schema: dict[str, object],
) -> dict[str, object]:
    address = parse_opentofu_address(_string(case.get("address"), "case address"))
    generation_values = _mapping(case.get("generation_values"), "generation values")
    snapshot = _mapping(case.get("snapshot"), "snapshot")
    module = _module(snapshot.get("module_source"))
    plan = _plan(address, snapshot)
    controller = _controller_snapshot(snapshot.get("record"), name_hint=address.name)
    projection = project_controller_snapshot(plan=plan, controller=controller, schema=schema)
    normalized = normalize_reconcile_snapshot(
        plan=plan,
        schema=schema,
        live=projection,
        module=module,
    )
    normalized = _bind_native_sources(normalized, module)
    reconcile_plan = build_reconcile_plan(normalized)
    reconcile_preview = render_reconcile(snapshot=normalized, plan=reconcile_plan)
    reconciliation = reconcile_outcome(reconcile_preview)

    cleaned = clean_resource(dict(generation_values), resource_schema)
    supported = set(generation_values) == set(cleaned)
    redacted_paths = strip_secret_shaped(cleaned)
    generated = _generate(
        address=address.absolute,
        name=address.name,
        values=generation_values,
        schema=schema,
    )
    generation = generate_outcome(generated)
    import_ids = [
        resource.import_id
        for resource in projection.resources
        if resource.address == address and resource.import_id is not None
    ]
    outcome = _outcome_name(
        plan=reconcile_plan,
        supported=supported,
        redacted_paths=redacted_paths,
    )
    return {
        "supported": supported,
        "plan_outcome": outcome,
        "import_ids": import_ids,
        "redacted_paths": redacted_paths,
        "generated_hcl_sha256": _generated_digest(generated),
        "receipt_input_digests": {
            "generate": [list(item) for item in generation.input_digests],
            "reconcile": [list(item) for item in reconciliation.input_digests],
        },
    }


def _read_corpus(path: Path) -> dict[str, object]:
    try:
        value = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("provider contract corpus is unreadable") from exc
    return _mapping(value, "corpus")


def _resource_schema(provider_schema: dict[str, Any]) -> dict[str, object]:
    providers = _mapping(provider_schema.get("provider_schemas"), "provider schemas")
    for provider in providers.values():
        provider_value = _mapping(provider, "provider schema")
        resources = _mapping(provider_value.get("resource_schemas"), "resource schemas")
        resource = resources.get("unifi_dns_record")
        if resource is not None:
            return _mapping(resource, "DNS resource schema")
    raise ValueError("provider contract corpus schema lacks DNS resource")


def _module(value: object) -> ModuleIndex:
    empty = ModuleIndex((), (), (), (), ())
    if value is None:
        return empty
    source = _string(value, "module source").encode()
    return reindex_module(empty, ((PurePosixPath("main.tf"), source),))


def _plan(address: OpenTofuAddress, snapshot: dict[str, object]) -> PlanDocument:
    state = _frozen_optional(snapshot.get("state"), "state")
    change_value = snapshot.get("change")
    change = None
    if change_value is not None:
        raw_change = _mapping(change_value, "change")
        change = ResourceChange(
            address,
            _action(_string(raw_change.get("action"), "change action")),
            _frozen_optional(raw_change.get("before"), "change before"),
            _frozen_optional(raw_change.get("after"), "change after"),
            _frozen_required(raw_change.get("after_unknown"), "change after unknown"),
        )
    live = _frozen_optional(snapshot.get("plan_live"), "plan live")
    return PlanDocument(
        (1, 0),
        StateDocument((), ()) if state is None else StateDocument(((address, state),), ()),
        () if change is None else (change,),
        () if live is None else ((address, live),),
    )


def _controller_snapshot(
    value: object, *, name_hint: str | None = None
) -> ControllerSnapshot:
    if value is None:
        return build_controller_snapshot(records=(), covered_resource_types=())
    record = _mapping(value, "controller record")
    import_id = _string(record.get("_id"), "controller record id")
    return build_controller_snapshot(
        records=(("unifi_dns_record", import_id, record),),
        covered_resource_types=("unifi_dns_record",),
        name_hints={("unifi_dns_record", import_id): name_hint} if name_hint else None,
    )


def _bind_native_sources(
    snapshot: ReconcileSnapshot, module: ModuleIndex
) -> ReconcileSnapshot:
    sources = {source.relative_path: source for source in module.sources}
    resources = {resource.address: resource for resource in module.resources}
    bound = []
    identities: dict[PurePosixPath, FileIdentity] = {}
    for observation in snapshot.resources:
        indexed = resources.get(observation.address.absolute)
        if indexed is None or indexed.block is None:
            bound.append(observation)
            continue
        source = sources[indexed.source_path]
        identity = FileIdentity(
            source.relative_path,
            0,
            0,
            0o100644,
            0,
            0,
            len(source.source),
            0,
            hashlib.sha256(source.source).hexdigest(),
        )
        values = observation.desired or observation.base
        if values is None:
            bound.append(observation)
            continue
        attributes = tuple(
            SourceAttribute(
                item.attribute_path,
                source.source[item.whole.start:item.whole.end],
                source.source[item.expression.start:item.expression.end],
                item.literal,
            )
            for item in indexed.attributes
        )
        committed = SourceResource(
            observation.address,
            identity,
            source.source[indexed.block.whole.start:indexed.block.whole.end],
            values,
            attributes,
        )
        identities[identity.relative_path] = identity
        bound.append(replace(observation, committed=committed))
    return replace(
        snapshot,
        resources=tuple(bound),
        source_identities=tuple(sorted(identities.values(), key=lambda item: item.relative_path)),
    )


def _generate(
    *,
    address: str,
    name: str,
    values: dict[str, object],
    schema: ProviderSchema,
) -> GeneratePreview:
    frozen = _frozen_required(values, "generation values")
    return render_generate(
        GenerateSnapshot(
            _controller_snapshot(None),
            schema,
            ModuleIndex((), (), (), (), ()),
            (),
            (),
            (),
            CoverageReport(),
            (),
            (GeneratedResource(address, "unifi_dns_record", name, frozen),),
        )
    )


def _generated_digest(preview: GeneratePreview) -> str:
    for candidate in preview.candidates:
        if candidate.relative_path == PurePosixPath("generated.tf"):
            assert candidate.candidate_sha256 is not None
            return candidate.candidate_sha256
    raise ValueError("provider contract corpus generation has no native HCL")


def _outcome_name(
    *, plan: ReconcilePlan, supported: bool, redacted_paths: list[str]
) -> str:
    if not supported:
        return "coverage-gap"
    if redacted_paths:
        return "redacted"
    if not plan.decisions:
        return "no-op"
    reason = plan.decisions[0].reason.value
    if reason == "live_resource_new":
        return "import"
    if reason == "live_only_change":
        return "update"
    if reason in {"no_change", "concurrent_change_converged"}:
        return "no-op"
    return reason


def _frozen_optional(value: object, label: str) -> FrozenObject | None:
    return None if value is None else _frozen_required(value, label)


def _action(value: str) -> ActionVector:
    for action in ActionVector:
        if action.value == (value,):
            return action
    raise ValueError("provider contract corpus change action is invalid")


def _frozen_required(value: object, label: str) -> FrozenObject:
    frozen = freeze_value(_mapping(value, label))
    assert isinstance(frozen, FrozenObject)
    return frozen


def _mapping(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise ValueError(f"provider contract corpus {label} is invalid")
    return value


def _string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"provider contract corpus {label} is invalid")
    return value
