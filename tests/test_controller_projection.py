import hashlib

import pytest

from ubitofu.controller_projection import project_controller_snapshot
from ubitofu.manifest import MANIFEST
from ubitofu.reconcile_model import (
    ActionVector,
    ControllerRecord,
    ControllerSnapshot,
    PlanDocument,
    ProviderSchema,
    ReasonCode,
    ResourceChange,
    StateDocument,
    parse_opentofu_address,
)
from ubitofu.values import FrozenObject, freeze_value


def _object(value):
    frozen = freeze_value(value)
    assert isinstance(frozen, FrozenObject)
    return frozen


def _identity(spec, token="synthetic-id"):
    if spec.id_rule == "mac":
        return {"mac": "02:00:00:00:00:01"}, "02:00:00:00:00:01"
    if spec.id_rule == "mac_or_id":
        return {"mac": "02:00:00:00:00:01"}, "02:00:00:00:00:01"
    if spec.id_rule == "site":
        return {"id": "synthetic-site"}, "synthetic-site"
    if spec.id_rule == "site:_id":
        return {"id": token, "site": "synthetic-site"}, f"synthetic-site:{token}"
    if spec.id_rule == "wg_two_level":
        return {"id": token, "network_id": "synthetic-network"}, f"synthetic-network:{token}"
    return {"id": token}, token


def _fixture(spec, *, raw_extra=None, provider_extra=None, schema_extra=None):
    provider_identity, import_id = _identity(spec)
    resource_type = spec.resource_type
    address = parse_opentofu_address(f"{resource_type}.synthetic")
    provider_values = {**provider_identity, "name": "synthetic", **(provider_extra or {})}
    change = ResourceChange(
        address,
        ActionVector.NOOP,
        _object(provider_values),
        _object(provider_values),
        _object({}),
    )
    plan = PlanDocument(
        (1, 0),
        StateDocument(((address, _object(provider_values)),)),
        (change,),
        ((address, change.before),),
    )
    raw_identity = {
        "_id": provider_identity.get("id"),
        "mac": provider_identity.get("mac"),
        "network_id": provider_identity.get("network_id"),
    }
    raw = {key: value for key, value in raw_identity.items() if value is not None}
    raw.update({"name": "synthetic", **(raw_extra or {})})
    controller = ControllerSnapshot(
        (ControllerRecord(resource_type, import_id, _object(raw)),),
        (resource_type,),
        hashlib.sha256(b"synthetic-controller").hexdigest(),
    )
    attributes = {
        "id": {"type": "string", "computed": True},
        "mac": {"type": "string", "optional": True},
        "network_id": {"type": "string", "optional": True},
        "site": {"type": "string", "optional": True},
        "name": {"type": "string", "optional": True},
        **(schema_extra or {}),
    }
    schema = ProviderSchema(((resource_type, _object({"block": {"attributes": attributes}})),))
    return plan, controller, schema


@pytest.mark.parametrize("spec", MANIFEST, ids=lambda spec: spec.resource_type)
def test_every_public_manifest_resource_projects_synthetic_controller_fields(spec):
    plan, controller, schema = _fixture(spec)

    projection = project_controller_snapshot(plan=plan, controller=controller, schema=schema)

    assert projection.blocking_reasons == ()
    assert len(projection.resources) == 1
    assert projection.resources[0].address == plan.changes[0].address
    assert ("name",) in projection.resources[0].comparable_paths


def test_projection_excludes_sensitive_and_computed_only_paths_explicitly():
    spec = next(spec for spec in MANIFEST if spec.resource_type == "unifi_wlan")
    plan, controller, schema = _fixture(
        spec,
        raw_extra={"passphrase": "synthetic-secret", "runtime": "controller-value"},
        provider_extra={"passphrase": None},
        schema_extra={
            "passphrase": {"type": "string", "optional": True, "sensitive": True},
            "runtime": {"type": "string", "computed": True},
        },
    )

    projection = project_controller_snapshot(plan=plan, controller=controller, schema=schema)

    paths = projection.resources[0].comparable_paths
    assert ("passphrase",) not in paths
    assert ("runtime",) not in paths
    assert "synthetic-secret" not in repr(projection.resources[0].values)


def test_projection_applies_manifest_owned_controller_field_coercion():
    spec = MANIFEST[0]
    plan, controller, schema = _fixture(
        spec,
        raw_extra={"enabled": "true"},
        provider_extra={"enabled": True},
        schema_extra={"enabled": {"type": "bool", "optional": True}},
    )

    projection = project_controller_snapshot(plan=plan, controller=controller, schema=schema)

    assert projection.resources[0].values == _object({"enabled": True, "name": "synthetic"})


def test_projection_blocks_unavailable_resource_type():
    spec = MANIFEST[0]
    plan, controller, schema = _fixture(spec)
    controller = ControllerSnapshot(controller.records, (), controller.canonical_sha256)

    projection = project_controller_snapshot(plan=plan, controller=controller, schema=schema)

    assert projection.resources == ()
    assert projection.blocking_reasons == (ReasonCode.STALE_CONTROLLER_OBSERVATION,)


def test_projection_blocks_duplicate_identity_match():
    spec = MANIFEST[0]
    plan, controller, schema = _fixture(spec)
    duplicate = ControllerSnapshot(
        (*controller.records, controller.records[0]),
        controller.covered_resource_types,
        controller.canonical_sha256,
    )

    projection = project_controller_snapshot(plan=plan, controller=duplicate, schema=schema)

    assert ReasonCode.INCOMPARABLE_CONTROLLER_OBSERVATION in projection.blocking_reasons


def test_projection_blocks_managed_provider_path_missing_from_controller_record():
    spec = MANIFEST[0]
    plan, controller, schema = _fixture(
        spec,
        provider_extra={"vlan": 10},
        schema_extra={"vlan": {"type": "number", "optional": True}},
    )

    projection = project_controller_snapshot(plan=plan, controller=controller, schema=schema)

    assert ReasonCode.INCOMPARABLE_CONTROLLER_OBSERVATION in projection.blocking_reasons


def test_projection_blocks_missing_nested_managed_leaf_even_when_sibling_projects():
    spec = MANIFEST[0]
    plan, controller, schema = _fixture(
        spec,
        raw_extra={"settings": {"visible": True}},
        provider_extra={"settings": {"required_value": 10, "visible": True}},
        schema_extra={
            "settings": {
                "optional": True,
                "nested_type": {
                    "nesting_mode": "single",
                    "attributes": {
                        "required_value": {"type": "number", "optional": True},
                        "visible": {"type": "bool", "optional": True},
                    },
                },
            }
        },
    )

    projection = project_controller_snapshot(plan=plan, controller=controller, schema=schema)

    assert ReasonCode.INCOMPARABLE_CONTROLLER_OBSERVATION in projection.blocking_reasons


def test_projection_is_deterministic_and_input_order_independent():
    first = MANIFEST[0]
    second = MANIFEST[1]
    plan_a, controller_a, schema_a = _fixture(first)
    plan_b, controller_b, schema_b = _fixture(second, raw_extra={"purpose": "wan"})
    plan = PlanDocument(
        (1, 0),
        StateDocument((*plan_b.prior_state.resources, *plan_a.prior_state.resources)),
        (*plan_b.changes, *plan_a.changes),
        (*plan_b.plan_time_live, *plan_a.plan_time_live),
    )
    controller = ControllerSnapshot(
        (*controller_b.records, *controller_a.records),
        (second.resource_type, first.resource_type),
        "input-digest",
    )
    schema = ProviderSchema((*schema_b.resources, *schema_a.resources))

    left = project_controller_snapshot(plan=plan, controller=controller, schema=schema)
    right = project_controller_snapshot(
        plan=PlanDocument(
            plan.format_version,
            StateDocument(tuple(reversed(plan.prior_state.resources))),
            tuple(reversed(plan.changes)),
            tuple(reversed(plan.plan_time_live)),
        ),
        controller=ControllerSnapshot(
            tuple(reversed(controller.records)),
            tuple(reversed(controller.covered_resource_types)),
            controller.canonical_sha256,
        ),
        schema=ProviderSchema(tuple(reversed(schema.resources))),
    )

    assert left == right
