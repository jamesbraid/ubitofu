import json
from pathlib import PurePosixPath

import pytest

from ubitofu.module_index import IndexedReference, ModuleIndex
from ubitofu.reconcile_model import (
    ActionVector,
    AddAttribute,
    AppendImport,
    AppendResource,
    CollectionIdentityPolicy,
    DeleteResource,
    Disposition,
    FileIdentity,
    InvalidSnapshot,
    LifecyclePolicy,
    ReasonCode,
    ReconcileSnapshot,
    RemoveAttribute,
    ResourceChange,
    ResourceObservation,
    SourceAnchor,
    SourceAttribute,
    SourceResource,
    UpdateScalar,
    parse_opentofu_address,
)
from ubitofu.reconcile_planner import build_reconcile_plan
from ubitofu.values import FrozenObject, freeze_value


def _object(value):
    if value is None:
        return None
    frozen = freeze_value(value)
    assert isinstance(frozen, FrozenObject)
    return frozen


def _source(address, attributes, *, path="main.tf", source_attributes=None):
    identity = FileIdentity(
        PurePosixPath(path), 1, 2, 0o100644, 3, 4, 10, 11, "a" * 64
    )
    if source_attributes is None:
        source_attributes = tuple(
            SourceAttribute(
                (name,),
                f"{name} = {json.dumps(value)}".encode(),
                json.dumps(value, sort_keys=True, separators=(",", ":")).encode(),
                True,
            )
            for name, value in sorted((attributes or {}).items())
        )
    return SourceResource(
        address,
        identity,
        b'resource "synthetic" "x" {}',
        _object(attributes),
        source_attributes,
    )


def _observation(
    *,
    base,
    desired,
    live,
    committed=True,
    action=ActionVector.UPDATE,
    resource_type="unifi_network",
    suffix="lan",
    lifecycle=None,
    identities=(),
    path="main.tf",
    blockers=(),
):
    address = parse_opentofu_address(f"{resource_type}.{suffix}")
    change = None
    if action is not None:
        change = ResourceChange(
            address,
            action,
            _object(live),
            _object(desired),
            _object({}),
        )
    return ResourceObservation(
        address,
        _source(address, desired or base or {}, path=path) if committed else None,
        _object(base),
        _object(desired),
        _object(live),
        change,
        lifecycle or LifecyclePolicy(False, "attention"),
        identities,
        blockers,
    )


def _plan(observation):
    return build_reconcile_plan(
        ReconcileSnapshot((observation,), ModuleIndex((), (), (), (), ()), (), "digest")
    )


@pytest.mark.parametrize(
    ("base", "desired", "live", "disposition", "reason"),
    [
        (1, 1, 1, Disposition.NO_CHANGE, ReasonCode.NO_CHANGE),
        (1, 2, 1, Disposition.PRESERVE_CODE, ReasonCode.CODE_ONLY_CHANGE),
        (1, 1, 2, Disposition.CAPTURE_LIVE, ReasonCode.LIVE_ONLY_CHANGE),
        (1, 2, 2, Disposition.NO_CHANGE, ReasonCode.CONCURRENT_CHANGE_CONVERGED),
        (1, 2, 3, Disposition.CONFLICT, ReasonCode.CONCURRENT_VALUE_CONFLICT),
    ],
)
def test_complete_scalar_three_way_table(base, desired, live, disposition, reason):
    decision = _plan(
        _observation(
            base={"vlan": base}, desired={"vlan": desired}, live={"vlan": live}
        )
    ).decisions[0]

    assert (decision.disposition, decision.reason) == (disposition, reason)
    if disposition is Disposition.CAPTURE_LIVE:
        assert len(decision.edits) == 1
        assert isinstance(decision.edits[0], UpdateScalar)
        assert decision.edits[0].anchor.attribute_path == ("vlan",)


def test_unknown_path_is_suppressed_before_normal_comparison():
    observation = _observation(base={"runtime": 1}, desired={"runtime": 2}, live={"runtime": 3})
    assert observation.change is not None
    observation = ResourceObservation(
        observation.address,
        observation.committed,
        observation.base,
        observation.desired,
        observation.live,
        ResourceChange(
            observation.address,
            ActionVector.UPDATE,
            observation.live,
            observation.desired,
            _object({"runtime": True}),
        ),
        observation.lifecycle,
        observation.collection_identities,
    )

    decision = _plan(observation).decisions[0]

    assert decision.disposition is Disposition.NO_CHANGE
    assert decision.reason is ReasonCode.COMPUTED_OR_UNKNOWN


def test_secret_shaped_path_is_suppressed_without_value_in_edits_or_messages():
    decision = _plan(
        _observation(
            base={"passphrase": "synthetic-old"},
            desired={"passphrase": None},
            live={"passphrase": "synthetic-new"},
        )
    ).decisions[0]

    assert decision.disposition is Disposition.NO_CHANGE
    assert decision.reason is ReasonCode.SECRET_SUPPRESSED
    assert "synthetic-new" not in repr(decision)


def test_absent_and_null_are_distinct_values():
    decision = _plan(
        _observation(base={}, desired={"vlan": None}, live={})
    ).decisions[0]

    assert decision.disposition is Disposition.PRESERVE_CODE
    assert decision.reason is ReasonCode.CODE_ONLY_CHANGE


@pytest.mark.parametrize(
    ("committed", "base", "desired", "live", "action", "disposition", "reason"),
    [
        (True, None, {"name": "lan"}, None, ActionVector.CREATE,
         Disposition.PRESERVE_CODE, ReasonCode.PENDING_CREATE),
        (False, {"name": "lan"}, None, {"name": "lan"}, ActionVector.DELETE,
         Disposition.PRESERVE_CODE, ReasonCode.PENDING_DELETE),
        (False, {"name": "lan"}, None, {"name": "lan"}, ActionVector.FORGET,
         Disposition.PRESERVE_CODE, ReasonCode.PENDING_FORGET),
        (False, {"name": "lan"}, None, {"name": "lan"}, None,
         Disposition.ATTENTION, ReasonCode.STATE_ORPHANED),
        (True, None, {"name": "lan"}, None, None,
         Disposition.ATTENTION, ReasonCode.COMMITTED_NOT_IN_STATE),
        (True, {"name": "lan"}, {"name": "lan"}, None, ActionVector.CREATE,
         Disposition.ATTENTION, ReasonCode.CONTROLLER_RESOURCE_DELETED),
    ],
)
def test_resource_existence_table(
    committed, base, desired, live, action, disposition, reason
):
    decision = _plan(
        _observation(
            committed=committed,
            base=base,
            desired=desired,
            live=live,
            action=action,
        )
    ).decisions[0]

    assert (decision.disposition, decision.reason) == (disposition, reason)


def test_new_live_resource_appends_typed_resource_and_import_intents():
    decision = _plan(
        _observation(
            committed=False,
            base=None,
            desired={"id": "synthetic-id", "name": "new"},
            live={"id": "synthetic-id", "name": "new"},
            action=ActionVector.NOOP,
            suffix="new",
        )
    ).decisions[0]

    assert decision.disposition is Disposition.APPEND
    assert decision.reason is ReasonCode.LIVE_RESOURCE_NEW
    assert any(isinstance(edit, AppendResource) for edit in decision.edits)
    assert any(isinstance(edit, AppendImport) for edit in decision.edits)


def test_ui_only_resource_create_is_forbidden():
    decision = _plan(
        _observation(
            committed=True,
            base=None,
            desired={"mac": "02:00:00:00:00:01"},
            live=None,
            action=ActionVector.CREATE,
            resource_type="unifi_device",
            suffix="switch",
            lifecycle=LifecyclePolicy(True, "capture"),
        )
    ).decisions[0]

    assert decision.disposition is Disposition.FORBIDDEN
    assert decision.reason is ReasonCode.FORBIDDEN_DEVICE_CREATE


def test_ui_only_controller_deletion_produces_typed_resource_delete():
    decision = _plan(
        _observation(
            committed=True,
            base={"mac": "02:00:00:00:00:01"},
            desired={"mac": "02:00:00:00:00:01"},
            live=None,
            action=ActionVector.CREATE,
            resource_type="unifi_device",
            suffix="switch",
            lifecycle=LifecyclePolicy(True, "capture"),
        )
    ).decisions[0]

    assert decision.disposition is Disposition.REMOVE
    assert decision.reason is ReasonCode.CONTROLLER_RESOURCE_DELETED
    assert isinstance(decision.edits[0], DeleteResource)


def test_resource_deletion_with_committed_reference_blocks_as_dangling():
    observation = _observation(
        committed=True,
        base={"mac": "02:00:00:00:00:01"},
        desired={"mac": "02:00:00:00:00:01"},
        live=None,
        action=ActionVector.CREATE,
        resource_type="unifi_device",
        suffix="switch",
        lifecycle=LifecyclePolicy(True, "capture"),
    )
    module = ModuleIndex(
        (),
        (),
        (),
        (),
        (IndexedReference(PurePosixPath("consumer.tf"), observation.address.absolute, None),),
    )
    snapshot = ReconcileSnapshot((observation,), module, (), "digest")

    decision = build_reconcile_plan(snapshot).decisions[0]

    assert decision.disposition is Disposition.ATTENTION
    assert decision.reason is ReasonCode.DANGLING_REFERENCE


@pytest.mark.parametrize("action", [ActionVector.DELETE_CREATE, ActionVector.CREATE_DELETE])
def test_replacement_requires_attention(action):
    decision = _plan(
        _observation(base={"vlan": 1}, desired={"vlan": 2}, live={"vlan": 1}, action=action)
    ).decisions[0]

    assert decision.disposition is Disposition.ATTENTION
    assert decision.reason is ReasonCode.REPLACEMENT_REQUIRES_ATTENTION


def test_delete_versus_live_modify_is_conflict():
    decision = _plan(
        _observation(
            committed=False,
            base={"vlan": 1},
            desired=None,
            live={"vlan": 2},
            action=ActionVector.DELETE,
        )
    ).decisions[0]

    assert decision.disposition is Disposition.CONFLICT
    assert decision.reason is ReasonCode.DELETE_MODIFY_CONFLICT


def test_json_owned_source_blocks_an_edit():
    decision = _plan(
        _observation(
            base={"vlan": 1},
            desired={"vlan": 1},
            live={"vlan": 2},
            path="main.tf.json",
        )
    ).decisions[0]

    assert decision.disposition is Disposition.ATTENTION
    assert decision.reason is ReasonCode.JSON_SOURCE_READ_ONLY


@pytest.mark.parametrize(
    "absolute",
    [
        "module.edge.unifi_network.lan",
        "unifi_network.lan[0]",
        'unifi_network.lan["blue"]',
        "data.unifi_network.lan",
    ],
)
def test_legal_but_unsupported_addresses_block_instead_of_disappearing(absolute):
    original = _observation(base={"vlan": 1}, desired={"vlan": 1}, live={"vlan": 2})
    address = parse_opentofu_address(absolute)
    observation = ResourceObservation(
        address,
        original.committed,
        original.base,
        original.desired,
        original.live,
        ResourceChange(address, ActionVector.UPDATE, original.live, original.desired, _object({})),
        original.lifecycle,
        original.collection_identities,
    )

    decision = _plan(observation).decisions[0]

    assert decision.address.absolute == absolute
    assert decision.disposition is Disposition.ATTENTION
    assert decision.reason is ReasonCode.UNSUPPORTED_ADDRESS


def test_expanded_instance_deletion_never_emits_whole_resource_delete():
    original = _observation(
        committed=True,
        base={"mac": "02:00:00:00:00:01"},
        desired={"mac": "02:00:00:00:00:01"},
        live=None,
        action=ActionVector.CREATE,
        resource_type="unifi_device",
        lifecycle=LifecyclePolicy(True, "capture"),
    )
    address = parse_opentofu_address('unifi_device.switch["one"]')
    observation = ResourceObservation(
        address,
        original.committed,
        original.base,
        original.desired,
        original.live,
        ResourceChange(address, ActionVector.CREATE, None, original.desired, _object({})),
        original.lifecycle,
        original.collection_identities,
    )

    plan = _plan(observation)

    assert plan.blocked is True
    assert plan.edits == ()
    assert plan.decisions[0].reason is ReasonCode.UNSUPPORTED_ADDRESS


def test_declared_keyed_collection_merges_independent_member_changes():
    decision = _plan(
        _observation(
            base={"items": [{"id": "a", "value": 1}, {"id": "b", "value": 1}]},
            desired={"items": [{"id": "a", "value": 2}, {"id": "b", "value": 1}]},
            live={"items": [{"id": "a", "value": 1}, {"id": "b", "value": 3}]},
            identities=(CollectionIdentityPolicy(("items",), "id"),),
        )
    ).decisions[0]

    assert decision.disposition is Disposition.CAPTURE_LIVE
    assert decision.reason is ReasonCode.LIVE_ONLY_CHANGE
    paths = [
        edit.anchor.attribute_path
        for edit in decision.edits
        if isinstance(edit, UpdateScalar)
    ]
    assert paths == [("items", "b", "value")]


def test_declared_keyed_map_merges_independent_member_changes():
    decision = _plan(
        _observation(
            base={"items": {"one": {"id": "a", "value": 1},
                            "two": {"id": "b", "value": 1}}},
            desired={"items": {"one": {"id": "a", "value": 2},
                               "two": {"id": "b", "value": 1}}},
            live={"items": {"one": {"id": "a", "value": 1},
                            "two": {"id": "b", "value": 3}}},
            identities=(CollectionIdentityPolicy(("items",), "id"),),
        )
    ).decisions[0]

    assert decision.disposition is Disposition.CAPTURE_LIVE
    assert decision.reason is ReasonCode.LIVE_ONLY_CHANGE


def test_unlisted_map_is_atomic_and_blocks_divergent_concurrent_change():
    decision = _plan(
        _observation(
            base={"settings": {"first": 1, "second": 1}},
            desired={"settings": {"first": 2, "second": 1}},
            live={"settings": {"first": 1, "second": 3}},
        )
    ).decisions[0]

    assert decision.disposition is Disposition.CONFLICT
    assert decision.reason is ReasonCode.CONCURRENT_VALUE_CONFLICT


@pytest.mark.parametrize(
    "values",
    [
        [{"id": "a", "value": 2}, {"id": "a", "value": 3}],
        [{"value": 2}],
    ],
)
def test_duplicate_or_missing_collection_identity_blocks(values):
    decision = _plan(
        _observation(
            base={"items": [{"id": "a", "value": 1}]},
            desired={"items": values},
            live={"items": [{"id": "a", "value": 4}]},
            identities=(CollectionIdentityPolicy(("items",), "id"),),
        )
    ).decisions[0]

    assert decision.disposition is Disposition.ATTENTION
    assert decision.reason is ReasonCode.UNSTABLE_COLLECTION_IDENTITY


def test_unlisted_positional_collection_blocks_divergent_concurrent_change():
    decision = _plan(
        _observation(base={"items": [1]}, desired={"items": [2]}, live={"items": [3]})
    ).decisions[0]

    assert decision.disposition is Disposition.CONFLICT
    assert decision.reason is ReasonCode.CONCURRENT_VALUE_CONFLICT


def test_projection_blocker_becomes_blocking_decision_and_suppresses_local_edit():
    observation = _observation(
        base={"vlan": 1},
        desired={"vlan": 1},
        live={"vlan": 2},
        blockers=(ReasonCode.INCOMPARABLE_CONTROLLER_OBSERVATION,),
    )

    plan = _plan(observation)

    assert plan.blocked is True
    assert plan.edits == ()
    assert plan.decisions[0].reason is ReasonCode.INCOMPARABLE_CONTROLLER_OBSERVATION


def test_duplicate_observation_is_invalid_internal_snapshot():
    observation = _observation(base={"vlan": 1}, desired={"vlan": 1}, live={"vlan": 1})
    snapshot = ReconcileSnapshot(
        (observation, observation), ModuleIndex((), (), (), (), ()), (), "digest"
    )

    with pytest.raises(InvalidSnapshot):
        build_reconcile_plan(snapshot)


def test_current_and_deposed_observations_share_display_address_without_collapsing():
    current = _observation(
        base={"vlan": 1}, desired={"vlan": 1}, live={"vlan": 2}
    )
    deposed_address = parse_opentofu_address(
        current.address.absolute, deposed="deadbeef"
    )
    deposed = ResourceObservation(
        deposed_address,
        None,
        _object({"vlan": 0}),
        _object({"vlan": 0}),
        _object({"vlan": 0}),
        ResourceChange(
            deposed_address,
            ActionVector.NOOP,
            _object({"vlan": 0}),
            _object({"vlan": 0}),
            _object({}),
        ),
        current.lifecycle,
        (),
    )
    snapshot = ReconcileSnapshot(
        (current, deposed), ModuleIndex((), (), (), (), ()), (), "digest"
    )

    plan = build_reconcile_plan(snapshot)

    assert [(decision.address.deposed, decision.reason) for decision in plan.decisions] == [
        (None, ReasonCode.LIVE_ONLY_CHANGE),
        ("deadbeef", ReasonCode.UNSUPPORTED_ADDRESS),
    ]
    assert plan.blocked is True
    assert plan.edits == ()


def test_controller_only_fresh_observation_is_planned_as_live_resource_new():
    address = parse_opentofu_address("unifi_network.guest_wifi")
    fresh = _object({"name": "guest"})
    observation = ResourceObservation(
        address,
        None,
        None,
        None,
        None,
        None,
        LifecyclePolicy(False, "attention"),
        (),
        (),
        True,
        fresh,
        "new-id",
    )

    plan = _plan(observation)

    assert plan.blocked is False
    assert plan.decisions[0].disposition is Disposition.APPEND
    assert plan.decisions[0].reason is ReasonCode.LIVE_RESOURCE_NEW
    assert plan.edits == (
        AppendImport(address, "new-id"),
        AppendResource(address, fresh),
    )


def _owned_observation(*, base, desired, live, source_attributes):
    observation = _observation(base=base, desired=desired, live=live)
    assert observation.committed is not None
    return ResourceObservation(
        observation.address,
        _source(
            observation.address,
            desired,
            source_attributes=source_attributes,
        ),
        observation.base,
        observation.desired,
        observation.live,
        observation.change,
        observation.lifecycle,
        observation.collection_identities,
    )


def test_literal_replace_anchors_the_exact_native_hcl_expression_bytes():
    observation = _owned_observation(
        base={"name": "lan"},
        desired={"name": "lan"},
        live={"name": "guest"},
        source_attributes=(
            SourceAttribute(("name",), b'name = "la\\u006e"', b'"la\\u006e"', True),
        ),
    )

    decision = _plan(observation).decisions[0]

    assert decision.edits == (
        UpdateScalar(
            observation.address,
            decision.edits[0].anchor,
            b'"guest"',
        ),
    )
    assert decision.edits[0].anchor.expected_literal == b'"la\\u006e"'


def test_attribute_add_uses_block_anchor_and_never_serializes_absence_as_null():
    observation = _owned_observation(
        base={}, desired={}, live={"vlan": 20}, source_attributes=()
    )

    decision = _plan(observation).decisions[0]

    assert decision.edits == (
        AddAttribute(
            observation.address,
            ("vlan",),
            SourceAnchor(observation.address, None, observation.committed.block_bytes),
            b"20",
        ),
    )


def test_attribute_remove_is_distinct_from_setting_literal_null():
    owned = (
        SourceAttribute(("vlan",), b"vlan = 10", b"10", True),
    )
    remove = _plan(
        _owned_observation(
            base={"vlan": 10}, desired={"vlan": 10}, live={}, source_attributes=owned
        )
    ).decisions[0]
    set_null = _plan(
        _owned_observation(
            base={"vlan": 10},
            desired={"vlan": 10},
            live={"vlan": None},
            source_attributes=owned,
        )
    ).decisions[0]

    assert isinstance(remove.edits[0], RemoveAttribute)
    assert remove.edits[0].anchor.expected_literal == b"10"
    assert isinstance(set_null.edits[0], UpdateScalar)
    assert set_null.edits[0].replacement == b"null"


@pytest.mark.parametrize(
    "source_attributes",
    [
        (SourceAttribute(("vlan",), b"vlan = var.vlan", b"var.vlan", False),),
        (),
        (
            SourceAttribute(("vlan",), b"vlan = 10", b"10", True),
            SourceAttribute(("vlan",), b"vlan = 11", b"11", True),
        ),
    ],
    ids=["variable-expression", "absent-owner", "ambiguous-owner"],
)
def test_nonliteral_missing_or_ambiguous_attribute_ownership_blocks(source_attributes):
    observation = _owned_observation(
        base={"vlan": 10},
        desired={"vlan": 10},
        live={"vlan": 20},
        source_attributes=source_attributes,
    )

    plan = _plan(observation)

    assert plan.blocked is True
    assert plan.edits == ()
    assert plan.decisions[0].disposition is Disposition.ATTENTION
    assert plan.decisions[0].reason is ReasonCode.SOURCE_OWNERSHIP_AMBIGUOUS
