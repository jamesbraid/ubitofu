import hashlib
from pathlib import PurePosixPath

from ubitofu.enumerator import EnumerationResult
from ubitofu.module_index import IndexedResource, IndexedSource, ModuleIndex, index_effective_module
from ubitofu.reconcile_model import (
    ActionVector,
    ControllerProjection,
    ControllerRecord,
    PlanDocument,
    ProjectedControllerResource,
    ProviderSchema,
    ReasonCode,
    ResourceChange,
    StateDocument,
    parse_opentofu_address,
)
from ubitofu.reconcile_snapshot import collect_reconcile_snapshot, normalize_reconcile_snapshot
from ubitofu.values import FrozenObject, freeze_value


def _object(value):
    frozen = freeze_value(value)
    assert isinstance(frozen, FrozenObject)
    return frozen


def _module(*, editable=True):
    source = IndexedSource(
        PurePosixPath("main.tf" if editable else "main.tf.json"),
        "native" if editable else "json",
        True,
        None,
        None,
    )
    resource = IndexedResource(
        "unifi_network.lan", source.relative_path, None, editable
    )
    return ModuleIndex((source,), (resource,), (), (), ())


def _inputs(*, projection_reasons=()):
    address = parse_opentofu_address("unifi_network.lan")
    change = ResourceChange(
        address,
        ActionVector.UPDATE,
        _object({"name": "lan", "vlan": 30, "runtime": "live"}),
        _object({"name": "lan", "vlan": 20, "runtime": "desired"}),
        _object({}),
    )
    plan = PlanDocument(
        (1, 0),
        StateDocument(((address, _object({"name": "lan", "vlan": 10, "runtime": "base"})),)),
        (change,),
        ((address, change.before),),
    )
    schema = ProviderSchema((("unifi_network", _object({"block": {"attributes": {}}})),))
    live = ControllerProjection(
        (
            ProjectedControllerResource(
                address,
                _object({"name": "controller-must-not-substitute", "vlan": 999}),
                (("name",), ("vlan",)),
            ),
        ),
        projection_reasons,
        "controller-digest",
    )
    return plan, schema, live


def test_normalization_uses_saved_plan_prior_after_and_before_only():
    plan, schema, live = _inputs()

    snapshot = normalize_reconcile_snapshot(
        plan=plan, schema=schema, live=live, module=_module()
    )

    observation = snapshot.resources[0]
    assert observation.base == _object({"name": "lan", "vlan": 10})
    assert observation.desired == _object({"name": "lan", "vlan": 20})
    assert observation.live == _object({"name": "lan", "vlan": 30})
    assert "999" not in repr(observation.live)
    assert snapshot.controller_digest == "controller-digest"


def test_normalization_retains_provider_identity_outside_comparable_paths_for_import():
    address = parse_opentofu_address("unifi_network.new")
    values = _object({"id": "synthetic-id", "name": "new"})
    plan = PlanDocument(
        (1, 0),
        StateDocument(()),
        (ResourceChange(address, ActionVector.NOOP, values, values, _object({})),),
        ((address, values),),
    )
    projection = ControllerProjection(
        (ProjectedControllerResource(address, _object({"name": "new"}), (("name",),)),),
        (),
        "digest",
    )

    snapshot = normalize_reconcile_snapshot(
        plan=plan,
        schema=ProviderSchema(()),
        live=projection,
        module=ModuleIndex((), (), (), (), ()),
    )

    assert snapshot.resources[0].desired == values
    assert snapshot.resources[0].live == values


def test_normalization_retains_manifest_lifecycle_and_collection_identity():
    address = parse_opentofu_address("unifi_device.switch")
    values = _object({"mac": "02:00:00:00:00:01", "name": "switch"})
    plan = PlanDocument(
        (1, 0),
        StateDocument(((address, values),)),
        (ResourceChange(address, ActionVector.NOOP, values, values, _object({})),),
        ((address, values),),
    )
    projection = ControllerProjection(
        (ProjectedControllerResource(address, values, (("mac",), ("name",))),),
        (),
        "digest",
    )
    module = ModuleIndex(
        (),
        (IndexedResource("unifi_device.switch", PurePosixPath("main.tf"), None, True),),
        (),
        (),
        (),
    )

    snapshot = normalize_reconcile_snapshot(
        plan=plan, schema=ProviderSchema(()), live=projection, module=module
    )

    observation = snapshot.resources[0]
    assert observation.lifecycle.create_in_ui_only is True
    assert observation.lifecycle.deletion_policy == "capture"
    assert observation.collection_identities[0].identity_attribute == "port_idx"


def test_normalization_retains_projection_blockers_for_planner():
    plan, schema, live = _inputs(
        projection_reasons=(ReasonCode.INCOMPARABLE_CONTROLLER_OBSERVATION,)
    )

    snapshot = normalize_reconcile_snapshot(
        plan=plan, schema=schema, live=live, module=_module()
    )

    assert snapshot.resources[0].blocking_reasons == (
        ReasonCode.INCOMPARABLE_CONTROLLER_OBSERVATION,
    )


def test_normalization_marks_json_owned_source_as_committed_but_read_only():
    plan, schema, live = _inputs()

    snapshot = normalize_reconcile_snapshot(
        plan=plan, schema=schema, live=live, module=_module(editable=False)
    )

    assert snapshot.resources[0].committed is not None
    assert snapshot.resources[0].committed.file.relative_path == PurePosixPath("main.tf.json")
    assert snapshot.resources[0].committed.block_bytes == b""


def test_normalization_orders_union_and_emits_one_observation_per_address():
    plan, schema, live = _inputs()
    extra = parse_opentofu_address("unifi_network.extra")
    plan = PlanDocument(
        plan.format_version,
        StateDocument((*plan.prior_state.resources, (extra, _object({"name": "extra"})))),
        plan.changes,
        plan.plan_time_live,
    )

    snapshot = normalize_reconcile_snapshot(
        plan=plan, schema=schema, live=live, module=_module()
    )

    assert [item.address.absolute for item in snapshot.resources] == [
        "unifi_network.extra",
        "unifi_network.lan",
    ]


def test_collector_uses_saved_plan_and_captures_exact_source_identity_without_state_query(
    monkeypatch, tmp_path
):
    source = b'resource "unifi_network" "lan" {\n  name = "lan"\n}\n'
    (tmp_path / "main.tf").write_bytes(source)
    module = index_effective_module(workdir=tmp_path)

    class Runner:
        workdir = tmp_path

        def plan(self, *, out=None, generate_config_out=None):
            assert out is not None
            assert generate_config_out is None
            return 0

        def show_json(self, path):
            return {
                "format_version": "1.0",
                "errored": False,
                "prior_state": {
                    "format_version": "1.0",
                    "values": {"root_module": {"resources": [{
                        "address": "unifi_network.lan",
                        "mode": "managed",
                        "type": "unifi_network",
                        "name": "lan",
                        "values": {"id": "synthetic-id", "name": "lan"},
                    }]}},
                },
                "resource_changes": [{
                    "address": "unifi_network.lan",
                    "mode": "managed",
                    "type": "unifi_network",
                    "name": "lan",
                    "change": {
                        "actions": ["no-op"],
                        "before": {"id": "synthetic-id", "name": "lan"},
                        "after": {"id": "synthetic-id", "name": "lan"},
                        "after_unknown": {},
                    },
                }],
            }

        def providers_schema(self):
            return {
                "format_version": "1.0",
                "provider_schemas": {"synthetic/provider": {"resource_schemas": {
                    "unifi_network": {"block": {"attributes": {
                        "id": {"type": "string", "computed": True},
                        "name": {"type": "string", "optional": True},
                    }}}
                }}},
            }

        def show_state_json(self):
            raise AssertionError("collector must not query current backend state")

    record = ControllerRecord(
        "unifi_network",
        "synthetic-id",
        _object({"_id": "synthetic-id", "name": "lan"}),
    )
    enumeration = EnumerationResult(
        records=[record], covered_resource_types=["unifi_network"]
    )
    monkeypatch.setattr(
        "ubitofu.reconcile_snapshot.enumerate_controller",
        lambda controller, *, capture_records: enumeration,
    )

    snapshot = collect_reconcile_snapshot(
        controller=object(), runner=Runner(), module=module
    )
    (tmp_path / "main.tf").write_bytes(b"changed after collection")

    committed = snapshot.resources[0].committed
    assert committed is not None
    assert committed.block_bytes == source.rstrip(b"\n")
    assert committed.file.sha256 == hashlib.sha256(source).hexdigest()
    assert committed.file.size == len(source)
