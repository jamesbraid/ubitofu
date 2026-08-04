# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 James Braid
import hashlib

import pytest

from ubitofu.controller_projection import project_controller_snapshot
from ubitofu.manifest import MANIFEST
from ubitofu.module_index import ModuleIndex
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
from ubitofu.reconcile_planner import build_reconcile_plan
from ubitofu.reconcile_snapshot import normalize_reconcile_snapshot
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


def _candidate_plan(*rows):
    changes = tuple(
        ResourceChange(address, ActionVector.NOOP, values, values, _object({}))
        for address, values in rows
    )
    return PlanDocument(
        (1, 0),
        StateDocument(tuple(rows)),
        changes,
        tuple(rows),
    )


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


def test_projection_compares_settable_optional_computed_attribute():
    spec = MANIFEST[0]
    plan, controller, schema = _fixture(
        spec,
        raw_extra={"vlan": 11},
        provider_extra={"vlan": 10},
        schema_extra={
            "vlan": {
                "type": "number",
                "optional": True,
                "computed": True,
            }
        },
    )

    projection = project_controller_snapshot(plan=plan, controller=controller, schema=schema)

    assert ("vlan",) in projection.resources[0].comparable_paths
    assert projection.blocking_reasons == ()
    assert (
        ReasonCode.STALE_CONTROLLER_OBSERVATION
        in projection.resources[0].blocking_reasons
    )


def test_nested_excluded_schema_leaves_never_influence_values_paths_or_digest():
    spec = next(spec for spec in MANIFEST if spec.resource_type == "unifi_wlan")
    nested_schema = {
        "settings": {
            "optional": True,
            "nested_type": {
                "nesting_mode": "list",
                "attributes": {
                    "visible": {"type": "string", "optional": True},
                    "opaque": {"type": "string", "optional": True, "sensitive": True},
                    "transient": {"type": "string", "optional": True, "write_only": True},
                    "settable": {"type": "string", "optional": True, "computed": True},
                    "derived": {"type": "string", "computed": True},
                },
            },
        }
    }
    provider = {"settings": [{"visible": "kept", "settable": "managed"}]}
    plan, controller, schema = _fixture(
        spec,
        raw_extra={
            "settings": [{
                "visible": "kept",
                "opaque": "first-sensitive",
                "transient": "first-write-only",
                "settable": "managed",
                "derived": "first-computed",
            }]
        },
        provider_extra=provider,
        schema_extra=nested_schema,
    )
    first = project_controller_snapshot(plan=plan, controller=controller, schema=schema)
    changed_record = ControllerRecord(
        controller.records[0].resource_type,
        controller.records[0].import_id,
        _object({
            "_id": "synthetic-id",
            "name": "synthetic",
            "settings": [{
                "visible": "kept",
                "opaque": "second-sensitive",
                "transient": "second-write-only",
                "settable": "managed",
                "derived": "second-computed",
            }],
        }),
    )
    second = project_controller_snapshot(
        plan=plan,
        controller=ControllerSnapshot(
            (changed_record,), controller.covered_resource_types, "changed-input"
        ),
        schema=schema,
    )

    assert first.resources[0].values == _object({
        "name": "synthetic",
        "settings": [{"settable": "managed", "visible": "kept"}],
    })
    assert first.resources[0].comparable_paths == (
        ("name",),
        ("settings", 0, "settable"),
        ("settings", 0, "visible"),
    )
    assert first.canonical_sha256 == second.canonical_sha256


@pytest.mark.parametrize(
    ("type_shape", "empty_value"),
    [
        (["map", "string"], {}),
        (["list", "string"], []),
        (["set", "string"], []),
    ],
    ids=["map", "list", "set"],
)
def test_explicit_empty_managed_collections_are_comparable_terminals(
    type_shape, empty_value
):
    spec = MANIFEST[0]
    plan, controller, schema = _fixture(
        spec,
        raw_extra={"members": empty_value},
        provider_extra={"members": empty_value},
        schema_extra={"members": {"type": type_shape, "optional": True}},
    )

    projection = project_controller_snapshot(plan=plan, controller=controller, schema=schema)

    assert projection.blocking_reasons == ()
    assert projection.resources[0].values == _object(
        {"members": empty_value, "name": "synthetic"}
    )
    assert ("members",) in projection.resources[0].comparable_paths


@pytest.mark.parametrize(
    ("type_shape", "empty_value"),
    [
        (["map", "string"], {}),
        (["list", "string"], []),
        (["set", "string"], []),
    ],
    ids=["map", "list", "set"],
)
def test_omitted_fresh_collection_does_not_compare_equal_to_explicit_empty(
    type_shape, empty_value
):
    spec = MANIFEST[0]
    plan, controller, schema = _fixture(
        spec,
        provider_extra={"members": empty_value},
        schema_extra={"members": {"type": type_shape, "optional": True}},
    )

    projection = project_controller_snapshot(plan=plan, controller=controller, schema=schema)

    assert projection.blocking_reasons == ()
    assert (
        ReasonCode.INCOMPARABLE_CONTROLLER_OBSERVATION
        in projection.resources[0].blocking_reasons
    )


def test_fresh_controller_change_after_plan_is_stale_on_the_same_comparable_paths():
    spec = MANIFEST[0]
    plan, controller, schema = _fixture(
        spec,
        raw_extra={"vlan": 20},
        provider_extra={"vlan": 10},
        schema_extra={"vlan": {"type": "number", "optional": True}},
    )

    projection = project_controller_snapshot(plan=plan, controller=controller, schema=schema)

    assert projection.blocking_reasons == ()
    assert (
        ReasonCode.STALE_CONTROLLER_OBSERVATION
        in projection.resources[0].blocking_reasons
    )


def test_controller_only_record_gets_deterministic_unreserved_synthetic_address():
    existing_address = parse_opentofu_address("unifi_network.guest_wifi")
    existing = _object({"id": "existing-id", "name": "existing"})
    plan = PlanDocument(
        (1, 0),
        StateDocument(((existing_address, existing),)),
        (ResourceChange(
            existing_address, ActionVector.NOOP, existing, existing, _object({})
        ),),
        ((existing_address, existing),),
    )
    controller = ControllerSnapshot(
        (
            ControllerRecord(
                "unifi_network",
                "existing-id",
                _object({"_id": "existing-id", "name": "existing"}),
                name_hint="Existing",
            ),
            ControllerRecord(
                "unifi_network",
                "new-id",
                _object({"_id": "new-id", "name": "guest"}),
                name_hint="Guest WiFi",
            ),
        ),
        ("unifi_network",),
        "controller",
    )
    schema = ProviderSchema(((
        "unifi_network",
        _object({"block": {"attributes": {
            "id": {"type": "string", "computed": True},
            "name": {"type": "string", "optional": True},
        }}}),
    ),))

    projection = project_controller_snapshot(plan=plan, controller=controller, schema=schema)

    assert projection.blocking_reasons == ()
    assert [resource.address.absolute for resource in projection.resources] == [
        "unifi_network.guest_wifi",
        "unifi_network.guest_wifi_2",
    ]
    fresh = projection.resources[1]
    assert fresh.import_id == "new-id"
    assert fresh.values == _object({"name": "guest"})


def _project_controller_only(
    resource_type,
    *,
    raw,
    attributes,
    block_types=None,
    import_id="new-id",
    name_hint="new resource",
):
    return project_controller_snapshot(
        plan=PlanDocument((1, 0), StateDocument(()), (), ()),
        controller=ControllerSnapshot(
            (
                ControllerRecord(
                    resource_type,
                    import_id,
                    _object(raw),
                    name_hint=name_hint,
                ),
            ),
            (resource_type,),
            "controller",
        ),
        schema=ProviderSchema(((
            resource_type,
            _object({"block": {
                "attributes": {
                    "id": {"type": "string", "computed": True},
                    **attributes,
                },
                "block_types": block_types or {},
            }}),
        ),)),
    )


def test_controller_only_projection_blocks_missing_required_plain_attribute():
    projection = _project_controller_only(
        "unifi_network",
        raw={"_id": "new-id"},
        attributes={"name": {"type": "string", "required": True}},
    )

    assert projection.blocking_reasons == ()
    assert projection.resources[0].blocking_reasons == (
        ReasonCode.INCOMPARABLE_CONTROLLER_OBSERVATION,
    )


def test_controller_only_blocker_does_not_contaminate_clean_managed_absence():
    deleted = parse_opentofu_address("unifi_device.deleted")
    deleted_values = _object({"mac": "02:00:00:00:00:01"})
    plan = PlanDocument(
        (1, 0),
        StateDocument(((deleted, deleted_values),)),
        (
            ResourceChange(
                deleted,
                ActionVector.CREATE,
                None,
                deleted_values,
                _object({}),
            ),
        ),
        ((deleted, None),),
    )
    controller = ControllerSnapshot(
        (
            ControllerRecord(
                "unifi_device",
                "02:00:00:00:00:02",
                _object({"mac": "02:00:00:00:00:02"}),
                "other",
            ),
        ),
        ("unifi_device",),
        hashlib.sha256(b"synthetic-controller").hexdigest(),
    )
    schema = ProviderSchema(
        ((
            "unifi_device",
            _object({
                "block": {
                    "attributes": {
                        "mac": {"type": "string", "required": True},
                        "name": {"type": "string", "required": True},
                    },
                },
            }),
        ),)
    )

    projection = project_controller_snapshot(
        plan=plan,
        controller=controller,
        schema=schema,
    )

    resources = {item.import_id: item for item in projection.resources}
    assert projection.blocking_reasons == ()
    assert resources["02:00:00:00:00:01"].present is False
    assert resources["02:00:00:00:00:01"].blocking_reasons == ()
    assert resources["02:00:00:00:00:02"].blocking_reasons == (
        ReasonCode.INCOMPARABLE_CONTROLLER_OBSERVATION,
    )


def test_controller_only_projection_blocks_unsourced_required_secret_without_copying_it():
    projection = _project_controller_only(
        "unifi_network",
        raw={"_id": "new-id", "name": "new", "credential": "synthetic-secret"},
        attributes={
            "name": {"type": "string", "required": True},
            "credential": {"type": "string", "required": True, "sensitive": True},
        },
    )

    assert projection.blocking_reasons == ()
    assert (
        ReasonCode.INCOMPARABLE_CONTROLLER_OBSERVATION
        in projection.resources[0].blocking_reasons
    )
    assert "synthetic-secret" not in repr(projection.resources[0].values)


def test_controller_only_projection_accepts_required_secret_with_renderer_owned_binding():
    projection = _project_controller_only(
        "unifi_wlan",
        raw={"_id": "new-id", "name": "new", "passphrase": "synthetic-secret"},
        attributes={
            "name": {"type": "string", "required": True},
            "passphrase": {"type": "string", "required": True, "sensitive": True},
        },
    )

    assert projection.blocking_reasons == ()
    assert projection.resources[0].blocking_reasons == ()
    assert projection.resources[0].values == _object({"name": "new"})
    assert "synthetic-secret" not in repr(projection)


@pytest.mark.parametrize(
    ("nesting_mode", "min_items", "entries", "blocked"),
    [
        ("list", 1, None, True),
        ("set", 1, [], True),
        ("list", 2, [{"host": "one"}], True),
        ("set", 2, [{"host": "one"}, {"host": "two"}], False),
        ("list", 1, [{}], True),
        ("set", 0, None, False),
    ],
    ids=[
        "absent-list",
        "empty-set",
        "undersized-list",
        "satisfied-set",
        "recursive-required-child",
        "optional-absent-set",
    ],
)
def test_controller_only_projection_enforces_required_nested_block_cardinality(
    nesting_mode, min_items, entries, blocked
):
    raw = {"_id": "new-id", "name": "new"}
    if entries is not None:
        raw["server"] = entries
    projection = _project_controller_only(
        "unifi_network",
        raw=raw,
        attributes={"name": {"type": "string", "required": True}},
        block_types={
            "server": {
                "nesting_mode": nesting_mode,
                "min_items": min_items,
                "max_items": 4,
                "block": {
                    "attributes": {
                        "host": {"type": "string", "required": True},
                        "port": {"type": "number", "optional": True},
                    }
                },
            }
        },
    )

    expected = (ReasonCode.INCOMPARABLE_CONTROLLER_OBSERVATION,) if blocked else ()
    assert projection.blocking_reasons == ()
    assert projection.resources[0].blocking_reasons == expected


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

    assert projection.blocking_reasons == ()
    assert len(projection.resources) == 1
    assert projection.resources[0].address == plan.changes[0].address
    assert projection.resources[0].blocking_reasons == (
        ReasonCode.STALE_CONTROLLER_OBSERVATION,
    )


def test_unsupported_candidate_type_is_blocked_at_its_known_address():
    address = parse_opentofu_address("terraform_data.synthetic")
    values = _object({"id": "synthetic-id"})
    plan = _candidate_plan((address, values))

    projection = project_controller_snapshot(
        plan=plan,
        controller=ControllerSnapshot((), (), "controller"),
        schema=ProviderSchema(()),
    )

    assert projection.blocking_reasons == ()
    assert len(projection.resources) == 1
    assert projection.resources[0].address == address
    assert projection.resources[0].blocking_reasons == (
        ReasonCode.INCOMPARABLE_CONTROLLER_OBSERVATION,
    )
    snapshot = normalize_reconcile_snapshot(
        plan=plan,
        schema=ProviderSchema(()),
        live=projection,
        module=ModuleIndex((), (), (), (), ()),
    )
    reconcile_plan = build_reconcile_plan(snapshot)
    assert reconcile_plan.blocked is True
    assert reconcile_plan.edits == ()
    assert reconcile_plan.decisions[0].address == address
    assert (
        reconcile_plan.decisions[0].reason
        is ReasonCode.INCOMPARABLE_CONTROLLER_OBSERVATION
    )


def test_candidate_with_missing_identity_is_blocked_at_its_known_address():
    address = parse_opentofu_address("unifi_network.synthetic")
    values = _object({"name": "synthetic"})

    projection = project_controller_snapshot(
        plan=_candidate_plan((address, values)),
        controller=ControllerSnapshot((), ("unifi_network",), "controller"),
        schema=ProviderSchema(()),
    )

    assert projection.blocking_reasons == ()
    assert len(projection.resources) == 1
    assert projection.resources[0].address == address
    assert projection.resources[0].blocking_reasons == (
        ReasonCode.INCOMPARABLE_CONTROLLER_OBSERVATION,
    )


def test_candidate_identity_error_is_blocked_at_its_known_address(monkeypatch):
    spec = MANIFEST[0]
    plan, controller, schema = _fixture(spec)

    def fail_identity(spec, values):
        raise ValueError("invalid identity")

    monkeypatch.setattr(
        "ubitofu.controller_projection._provider_identity",
        fail_identity,
    )
    controller = ControllerSnapshot((), controller.covered_resource_types, "controller")

    projection = project_controller_snapshot(
        plan=plan,
        controller=controller,
        schema=schema,
    )

    assert projection.blocking_reasons == ()
    assert len(projection.resources) == 1
    assert projection.resources[0].address == plan.changes[0].address
    assert projection.resources[0].blocking_reasons == (
        ReasonCode.INCOMPARABLE_CONTROLLER_OBSERVATION,
    )


@pytest.mark.parametrize("controller_present", [False, True])
def test_duplicate_matching_candidates_are_each_blocked_locally(controller_present):
    spec = MANIFEST[0]
    plan, controller, schema = _fixture(spec)
    original = plan.changes[0]
    sibling = parse_opentofu_address(f"{spec.resource_type}.sibling")
    duplicate_plan = _candidate_plan(
        (original.address, original.after),
        (sibling, original.after),
    )

    projection = project_controller_snapshot(
        plan=duplicate_plan,
        controller=ControllerSnapshot(
            controller.records if controller_present else (),
            controller.covered_resource_types,
            controller.canonical_sha256,
        ),
        schema=schema,
    )

    assert projection.blocking_reasons == ()
    assert {item.address for item in projection.resources} == {
        original.address,
        sibling,
    }
    assert all(
        item.blocking_reasons
        == (ReasonCode.INCOMPARABLE_CONTROLLER_OBSERVATION,)
        for item in projection.resources
    )


def test_projection_blocks_duplicate_identity_match():
    spec = MANIFEST[0]
    plan, controller, schema = _fixture(spec)
    duplicate = ControllerSnapshot(
        (*controller.records, controller.records[0]),
        controller.covered_resource_types,
        controller.canonical_sha256,
    )

    projection = project_controller_snapshot(plan=plan, controller=duplicate, schema=schema)

    assert projection.blocking_reasons == ()
    assert len(projection.resources) == 1
    assert projection.resources[0].address == plan.changes[0].address
    assert projection.resources[0].blocking_reasons == (
        ReasonCode.INCOMPARABLE_CONTROLLER_OBSERVATION,
    )


@pytest.mark.parametrize("schema_case", ["missing", "invalid"])
def test_matched_schema_failure_is_blocked_at_candidate_address(schema_case):
    spec = MANIFEST[0]
    plan, controller, _ = _fixture(spec)
    schema = (
        ProviderSchema(())
        if schema_case == "missing"
        else ProviderSchema(((
            spec.resource_type,
            _object({"block": {"attributes": {"name": "invalid"}}}),
        ),))
    )

    projection = project_controller_snapshot(
        plan=plan,
        controller=controller,
        schema=schema,
    )

    assert projection.blocking_reasons == ()
    assert len(projection.resources) == 1
    assert projection.resources[0].address == plan.changes[0].address
    assert projection.resources[0].blocking_reasons == (
        ReasonCode.INCOMPARABLE_CONTROLLER_OBSERVATION,
    )


def test_unmatched_schema_failure_remains_projection_wide():
    spec = MANIFEST[0]
    _, controller, _ = _fixture(spec)
    empty_plan = PlanDocument((1, 0), StateDocument(()), (), ())

    projection = project_controller_snapshot(
        plan=empty_plan,
        controller=controller,
        schema=ProviderSchema(()),
    )

    assert projection.resources == ()
    assert projection.blocking_reasons == (
        ReasonCode.INCOMPARABLE_CONTROLLER_OBSERVATION,
    )


def test_unmatched_global_failure_still_blocks_clean_candidate_plan():
    spec = MANIFEST[0]
    plan, controller, schema = _fixture(spec)
    unrelated = ControllerRecord(
        "unifi_wlan",
        "synthetic-unmatched-id",
        _object({"_id": "synthetic-unmatched-id", "name": "unmatched"}),
        "unmatched",
    )
    controller = ControllerSnapshot(
        (*controller.records, unrelated),
        (*controller.covered_resource_types, "unifi_wlan"),
        "controller",
    )

    projection = project_controller_snapshot(
        plan=plan,
        controller=controller,
        schema=schema,
    )
    snapshot = normalize_reconcile_snapshot(
        plan=plan,
        schema=schema,
        live=projection,
        module=ModuleIndex((), (), (), (), ()),
    )
    reconcile_plan = build_reconcile_plan(snapshot)

    assert projection.blocking_reasons == (
        ReasonCode.INCOMPARABLE_CONTROLLER_OBSERVATION,
    )
    assert reconcile_plan.blocked is True
    assert reconcile_plan.edits == ()
    assert reconcile_plan.decisions[0].reason is (
        ReasonCode.INCOMPARABLE_CONTROLLER_OBSERVATION
    )


def test_projection_blocks_managed_provider_path_missing_from_controller_record():
    spec = MANIFEST[0]
    plan, controller, schema = _fixture(
        spec,
        provider_extra={"vlan": 10},
        schema_extra={"vlan": {"type": "number", "optional": True}},
    )

    projection = project_controller_snapshot(plan=plan, controller=controller, schema=schema)

    assert projection.blocking_reasons == ()
    assert (
        ReasonCode.INCOMPARABLE_CONTROLLER_OBSERVATION
        in projection.resources[0].blocking_reasons
    )


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

    assert projection.blocking_reasons == ()
    assert (
        ReasonCode.INCOMPARABLE_CONTROLLER_OBSERVATION
        in projection.resources[0].blocking_reasons
    )


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
