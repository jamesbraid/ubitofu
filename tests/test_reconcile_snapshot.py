import hashlib
import json
from pathlib import PurePosixPath

import pytest

from ubitofu.enumerator import EnumerationResult, ImportTarget
from ubitofu.errors import ExternalDocumentError
from ubitofu.module_index import IndexedResource, IndexedSource, ModuleIndex, index_effective_module
from ubitofu.outcomes import decode_receipt, reconcile_outcome, render_human, render_json
from ubitofu.reconcile_model import (
    ActionVector,
    ControllerProjection,
    ControllerRecord,
    PlanDocument,
    ProjectedControllerResource,
    ProviderSchema,
    ReasonCode,
    ResourceChange,
    SecretChangeKind,
    StateDocument,
    parse_opentofu_address,
)
from ubitofu.reconcile_planner import build_reconcile_plan
from ubitofu.reconcile_renderer import ReconcilePreview
from ubitofu.reconcile_snapshot import collect_reconcile_snapshot, normalize_reconcile_snapshot
from ubitofu.tofu_json import parse_provider_schema
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


def test_normalization_classifies_sensitive_values_then_discards_them():
    address = parse_opentofu_address("unifi_wlan.wifi")
    base = _object({"name": "wifi", "passphrase": "synthetic-base"})
    desired = _object({"name": "wifi", "passphrase": "synthetic-code"})
    live = _object({"name": "wifi", "passphrase": "synthetic-ui"})
    mask = _object({"passphrase": True})
    change = ResourceChange(
        address,
        ActionVector.UPDATE,
        live,
        desired,
        _object({}),
        mask,
        mask,
    )
    plan = PlanDocument(
        (1, 0), StateDocument(((address, base),)), (change,), ((address, live),)
    )
    schema = ProviderSchema((("unifi_wlan", _object({"block": {"attributes": {
        "name": {"type": "string", "optional": True},
        "passphrase": {"type": "string", "optional": True, "sensitive": True},
    }}})),))
    projection = ControllerProjection((ProjectedControllerResource(
        address, _object({"name": "wifi"}), (("name",),)
    ),), (), "safe-controller-digest")

    snapshot = normalize_reconcile_snapshot(
        plan=plan, schema=schema, live=projection, module=_module()
    )

    observation = next(item for item in snapshot.resources if item.address == address)
    assert observation.secret_changes[0].path == ("passphrase",)
    assert observation.secret_changes[0].kind is SecretChangeKind.CONFLICT
    assert observation.secret_changes[0].comparable is True
    assert observation.base == _object({"name": "wifi"})
    assert observation.desired == _object({"name": "wifi"})
    assert observation.live == _object({"name": "wifi"})
    assert "synthetic-" not in repr(snapshot)


def test_normalization_marks_write_only_secret_fact_as_incomparable_without_value():
    address = parse_opentofu_address("unifi_wlan.wifi")
    base = _object({"name": "wifi", "passphrase_wo": None})
    desired = _object({"name": "wifi", "passphrase_wo": "synthetic-code"})
    live = _object({"name": "wifi", "passphrase_wo": None})
    mask = _object({"passphrase_wo": True})
    change = ResourceChange(
        address, ActionVector.UPDATE, live, desired, _object({}), mask, mask
    )
    plan = PlanDocument(
        (1, 0), StateDocument(((address, base),)), (change,), ((address, live),)
    )
    schema = ProviderSchema((("unifi_wlan", _object({"block": {"attributes": {
        "name": {"type": "string", "optional": True},
        "passphrase_wo": {"type": "string", "optional": True, "write_only": True},
    }}})),))
    projection = ControllerProjection((ProjectedControllerResource(
        address, _object({"name": "wifi"}), (("name",),)
    ),), (), "safe-controller-digest")

    snapshot = normalize_reconcile_snapshot(
        plan=plan, schema=schema, live=projection, module=_module()
    )

    observation = next(item for item in snapshot.resources if item.address == address)
    fact = observation.secret_changes[0]
    assert fact.kind is SecretChangeKind.CODE_ONLY
    assert fact.comparable is False
    assert "synthetic-code" not in repr(snapshot)


def test_normalization_scrubs_secret_values_without_a_controller_projection():
    address = parse_opentofu_address("unifi_network.lan")
    base = _object({"name": "lan", "credential": "synthetic-base-secret"})
    desired = _object({"name": "lan", "credential": "synthetic-code-secret"})
    live = _object({"name": "lan", "credential": "synthetic-live-secret"})
    mask = _object({"credential": True})
    change = ResourceChange(address, ActionVector.UPDATE, live, desired, _object({}))
    plan = PlanDocument(
        (1, 0),
        StateDocument(((address, base),), ((address, mask),)),
        (change,),
        ((address, live),),
    )
    schema = ProviderSchema((("unifi_network", _object({"block": {"attributes": {
        "name": {"type": "string", "optional": True},
        "credential": {"type": "string", "optional": True},
    }}})),))

    snapshot = normalize_reconcile_snapshot(
        plan=plan,
        schema=schema,
        live=ControllerProjection((), (), "safe-controller-digest"),
        module=_module(),
    )

    observation = snapshot.resources[0]
    assert observation.base == _object({"name": "lan"})
    assert observation.desired == _object({"name": "lan"})
    assert observation.live == _object({"name": "lan"})
    assert observation.change is not None
    assert observation.change.before == _object({"name": "lan"})
    assert observation.change.after == _object({"name": "lan"})
    assert observation.committed is not None
    assert observation.committed.attributes == _object({"name": "lan"})
    assert "synthetic-" not in repr(snapshot)


def test_normalization_scrubs_nested_block_type_secret_values():
    address = parse_opentofu_address("unifi_network.lan")
    base = _object({"name": "lan", "auth": [{"token": "synthetic-base"}]})
    desired = _object({"name": "lan", "auth": [{"token": "synthetic-code"}]})
    live = _object({"name": "lan", "auth": [{"token": "synthetic-ui"}]})
    change = ResourceChange(address, ActionVector.UPDATE, live, desired, _object({}))
    plan = PlanDocument(
        (1, 0), StateDocument(((address, base),)), (change,), ((address, live),)
    )
    schema = ProviderSchema((("unifi_network", _object({"block": {
        "attributes": {"name": {"type": "string", "optional": True}},
        "block_types": {"auth": {
            "nesting_mode": "list",
            "block": {"attributes": {
                "token": {"type": "string", "optional": True, "sensitive": True},
            }},
        }},
    }})),))

    snapshot = normalize_reconcile_snapshot(
        plan=plan,
        schema=schema,
        live=ControllerProjection((), (), "safe-controller-digest"),
        module=_module(),
    )

    observation = snapshot.resources[0]
    assert observation.secret_changes[0].path == ("auth", 0, "token")
    assert observation.secret_changes[0].kind is SecretChangeKind.CONFLICT
    assert "synthetic-" not in repr(snapshot)


@pytest.mark.parametrize("dynamic_key", ["tenant.one", "tenant_secret", "tenantone"])
def test_nested_sensitive_map_keys_are_wildcarded_in_public_conflict_receipts(
    dynamic_key,
):
    address = parse_opentofu_address("unifi_network.lan")
    secrets = (
        "synthetic-map-base",
        "synthetic-map-code",
        "synthetic-map-controller",
    )
    base = _object({"credentials": {dynamic_key: {"token": secrets[0]}}})
    desired = _object({"credentials": {dynamic_key: {"token": secrets[1]}}})
    live = _object({"credentials": {dynamic_key: {"token": secrets[2]}}})
    change = ResourceChange(address, ActionVector.UPDATE, live, desired, _object({}))
    plan_document = PlanDocument(
        (1, 0), StateDocument(((address, base),)), (change,), ((address, live),)
    )
    schema = ProviderSchema((('unifi_network', _object({"block": {
        "block_types": {"credentials": {
            "nesting_mode": "map",
            "block": {"attributes": {
                "token": {"type": "string", "optional": True, "sensitive": True},
            }},
        }},
    }})),))

    snapshot = normalize_reconcile_snapshot(
        plan=plan_document,
        schema=schema,
        live=ControllerProjection((), (), "b" * 64),
        module=_module(),
    )
    plan = build_reconcile_plan(snapshot)
    decision = plan.decisions[0]

    assert snapshot.resources[0].secret_changes[0].path == (
        "credentials",
        "*",
        "token",
    )
    assert decision.conflict_paths == (("credentials", "*", "token"),)

    outcome = reconcile_outcome(ReconcilePreview(snapshot, plan, (), (), ()))
    human = render_human(outcome)
    raw = render_json(outcome)
    receipt = decode_receipt(raw)
    item = next(
        item
        for item in json.loads(raw)["outcome"]["items"]
        if item["reason_code"] == "concurrent_secret_conflict"
    )
    assert item["attribute_paths"] == [["credentials", "*", "token"]]
    assert "credentials[*].token" in human
    assert receipt.outcome == outcome
    for forbidden in (dynamic_key, *secrets):
        digest = hashlib.sha256(forbidden.encode()).hexdigest()
        assert forbidden not in human
        assert forbidden.encode() not in raw
        assert digest.encode() not in raw


def test_nested_sensitive_set_indexes_are_wildcarded_in_public_conflict_paths():
    address = parse_opentofu_address("unifi_network.lan")
    base = _object({"credentials": [{"name": "tenant.one", "token": "set-base"}]})
    desired = _object({"credentials": [{"name": "tenant.one", "token": "set-code"}]})
    live = _object({"credentials": [{"name": "tenant.one", "token": "set-live"}]})
    change = ResourceChange(address, ActionVector.UPDATE, live, desired, _object({}))
    plan_document = PlanDocument(
        (1, 0), StateDocument(((address, base),)), (change,), ((address, live),)
    )
    schema = ProviderSchema((('unifi_network', _object({"block": {
        "block_types": {"credentials": {
            "nesting_mode": "set",
            "block": {"attributes": {
                "name": {"type": "string", "optional": True},
                "token": {"type": "string", "optional": True, "sensitive": True},
            }},
        }},
    }})),))

    snapshot = normalize_reconcile_snapshot(
        plan=plan_document,
        schema=schema,
        live=ControllerProjection((), (), "b" * 64),
        module=_module(),
    )
    decision = build_reconcile_plan(snapshot).decisions[0]

    assert snapshot.resources[0].secret_changes[0].path == (
        "credentials",
        "*",
        "token",
    )
    assert decision.conflict_paths == (("credentials", "*", "token"),)


@pytest.mark.parametrize("dynamic_key", ["tenant.one", "tenant_secret", "tenantone"])
def test_plain_sensitive_map_attribute_keys_are_wildcarded_in_public_receipts(
    dynamic_key,
):
    address = parse_opentofu_address("unifi_network.lan")
    secrets = ("plain-base", "plain-code", "plain-live")
    base = _object({"credentials": {dynamic_key: secrets[0]}})
    desired = _object({"credentials": {dynamic_key: secrets[1]}})
    live = _object({"credentials": {dynamic_key: secrets[2]}})
    mask = _object({"credentials": {dynamic_key: True}})
    change = ResourceChange(
        address,
        ActionVector.UPDATE,
        live,
        desired,
        _object({}),
        before_sensitive=mask,
        after_sensitive=mask,
    )
    plan_document = PlanDocument(
        (1, 0), StateDocument(((address, base),)), (change,), ((address, live),)
    )
    schema = ProviderSchema((('unifi_network', _object({"block": {"attributes": {
        "credentials": {"type": ["map", "string"], "optional": True},
    }}})),))

    snapshot = normalize_reconcile_snapshot(
        plan=plan_document,
        schema=schema,
        live=ControllerProjection((), (), "b" * 64),
        module=_module(),
    )
    plan = build_reconcile_plan(snapshot)

    assert snapshot.resources[0].secret_changes[0].path == ("credentials", "*")
    assert plan.decisions[0].conflict_paths == (("credentials", "*"),)
    outcome = reconcile_outcome(ReconcilePreview(snapshot, plan, (), (), ()))
    human = render_human(outcome)
    raw = render_json(outcome)
    assert decode_receipt(raw).outcome == outcome
    assert "credentials[*]" in human
    for forbidden in (dynamic_key, *secrets):
        digest = hashlib.sha256(forbidden.encode()).hexdigest()
        assert forbidden not in human
        assert forbidden.encode() not in raw
        assert digest.encode() not in raw


def test_plain_cty_nested_map_set_list_and_object_paths_use_only_dynamic_wildcards():
    address = parse_opentofu_address("unifi_network.lan")
    dynamic_key = "tenant.one"
    secrets = ("nested-base", "nested-code", "nested-live")

    def values(token):
        return _object({
            "settings": {
                "by_tenant": {
                    dynamic_key: [{"tokens": [token]}],
                },
            },
        })

    mask = _object({
        "settings": {
            "by_tenant": {
                dynamic_key: [{"tokens": [True]}],
            },
        },
    })
    base, desired, live = (values(secret) for secret in secrets)
    change = ResourceChange(
        address,
        ActionVector.UPDATE,
        live,
        desired,
        _object({}),
        before_sensitive=mask,
        after_sensitive=mask,
    )
    type_expression = ["object", {
        "by_tenant": ["map", ["list", ["object", {
            "tokens": ["set", "string"],
        }]]],
    }]
    schema = ProviderSchema((('unifi_network', _object({"block": {"attributes": {
        "settings": {"type": type_expression, "optional": True},
    }}})),))
    snapshot = normalize_reconcile_snapshot(
        plan=PlanDocument(
            (1, 0), StateDocument(((address, base),)), (change,), ((address, live),)
        ),
        schema=schema,
        live=ControllerProjection((), (), "b" * 64),
        module=_module(),
    )
    plan = build_reconcile_plan(snapshot)
    expected = ("settings", "by_tenant", "*", 0, "tokens", "*")

    assert snapshot.resources[0].secret_changes[0].path == expected
    assert plan.decisions[0].conflict_paths == (expected,)
    outcome = reconcile_outcome(ReconcilePreview(snapshot, plan, (), (), ()))
    human = render_human(outcome)
    raw = render_json(outcome)
    assert "settings.by_tenant[*][0].tokens[*]" in human
    assert decode_receipt(raw).outcome == outcome
    for forbidden in (dynamic_key, *secrets):
        digest = hashlib.sha256(forbidden.encode()).hexdigest()
        assert forbidden not in human
        assert forbidden.encode() not in raw
        assert digest.encode() not in raw


def test_plain_cty_tuple_positions_and_object_names_remain_static_around_wildcards():
    address = parse_opentofu_address("unifi_network.lan")
    dynamic_key = "tenant.one"

    def values(first, second):
        return _object({
            "settings": [
                {dynamic_key: first},
                {"fixed": [second]},
            ],
        })

    mask = _object({
        "settings": [
            {dynamic_key: True},
            {"fixed": [True]},
        ],
    })
    base = values("tuple-base", "set-base")
    desired = values("tuple-code", "set-code")
    live = values("tuple-live", "set-live")
    change = ResourceChange(
        address,
        ActionVector.UPDATE,
        live,
        desired,
        _object({}),
        before_sensitive=mask,
        after_sensitive=mask,
    )
    type_expression = ["tuple", [
        ["map", "string"],
        ["object", {"fixed": ["set", "string"]}],
    ]]
    schema = ProviderSchema((('unifi_network', _object({"block": {"attributes": {
        "settings": {"type": type_expression, "optional": True},
    }}})),))
    snapshot = normalize_reconcile_snapshot(
        plan=PlanDocument(
            (1, 0), StateDocument(((address, base),)), (change,), ((address, live),)
        ),
        schema=schema,
        live=ControllerProjection((), (), "b" * 64),
        module=_module(),
    )
    expected = (
        ("settings", 0, "*"),
        ("settings", 1, "fixed", "*"),
    )

    assert tuple(fact.path for fact in snapshot.resources[0].secret_changes) == expected
    assert build_reconcile_plan(snapshot).decisions[0].conflict_paths == expected


def test_malformed_consumed_cty_type_fails_before_sensitive_map_key_is_public():
    dynamic_key = "tenant.one"
    raw_schema = {
        "format_version": "1.0",
        "provider_schemas": {"synthetic/provider": {"resource_schemas": {
            "unifi_network": {"block": {"attributes": {
                "credentials": {"type": ["map"], "optional": True},
            }}},
        }}},
    }
    mask = _object({"credentials": {dynamic_key: True}})
    address = parse_opentofu_address("unifi_network.lan")
    values = _object({"credentials": {dynamic_key: "synthetic-secret"}})
    change = ResourceChange(
        address,
        ActionVector.UPDATE,
        values,
        values,
        _object({}),
        before_sensitive=mask,
        after_sensitive=mask,
    )
    plan = PlanDocument(
        (1, 0), StateDocument(((address, values),)), (change,), ((address, values),)
    )
    assert plan.changes[0].before_sensitive == mask

    with pytest.raises(ExternalDocumentError) as exc_info:
        parse_provider_schema(raw_schema)

    assert (
        exc_info.value.kind,
        exc_info.value.field,
        exc_info.value.reason,
    ) == ("provider_schema", "type", "invalid document")
    assert dynamic_key not in str(exc_info.value)


def test_provider_attribute_requires_a_type_or_nested_type():
    attribute = {"optional": True}
    raw_schema = {
        "format_version": "1.0",
        "provider_schemas": {"synthetic/provider": {"resource_schemas": {
            "unifi_network": {"block": {"attributes": {
                "credentials": attribute,
            }}},
        }}},
    }

    with pytest.raises(ExternalDocumentError) as exc_info:
        parse_provider_schema(raw_schema)

    assert (
        exc_info.value.kind,
        exc_info.value.field,
        exc_info.value.reason,
    ) == ("provider_schema", "attribute", "invalid document")


def test_provider_nested_type_takes_priority_when_type_is_also_present():
    raw_schema = {
        "format_version": "1.0",
        "provider_schemas": {"synthetic/provider": {"resource_schemas": {
            "unifi_network": {"block": {"attributes": {
                "credentials": {
                    "type": ["malformed-cty-type"],
                    "nested_type": {
                        "nesting_mode": "single",
                        "attributes": {
                            "value": {"type": "string", "optional": True},
                        },
                    },
                    "optional": True,
                },
            }}},
        }}},
    }

    parsed = parse_provider_schema(raw_schema)

    assert parsed.resources[0][0] == "unifi_network"


@pytest.mark.parametrize("dynamic_key", ["tenant.one", "tenant_secret", "tenantone"])
def test_dynamic_cty_map_keys_are_conservatively_wildcarded_in_public_receipts(
    dynamic_key,
):
    address = parse_opentofu_address("unifi_network.lan")
    secrets = ("dynamic-base", "dynamic-code", "dynamic-live")
    base = _object({"credentials": {dynamic_key: secrets[0]}})
    desired = _object({"credentials": {dynamic_key: secrets[1]}})
    live = _object({"credentials": {dynamic_key: secrets[2]}})
    mask = _object({"credentials": {dynamic_key: True}})
    change = ResourceChange(
        address,
        ActionVector.UPDATE,
        live,
        desired,
        _object({}),
        before_sensitive=mask,
        after_sensitive=mask,
    )
    schema = ProviderSchema((('unifi_network', _object({"block": {"attributes": {
        "credentials": {"type": "dynamic", "optional": True},
    }}})),))
    snapshot = normalize_reconcile_snapshot(
        plan=PlanDocument(
            (1, 0), StateDocument(((address, base),)), (change,), ((address, live),)
        ),
        schema=schema,
        live=ControllerProjection((), (), "b" * 64),
        module=_module(),
    )
    plan = build_reconcile_plan(snapshot)

    assert snapshot.resources[0].secret_changes[0].path == ("credentials", "*")
    assert plan.decisions[0].conflict_paths == (("credentials", "*"),)
    outcome = reconcile_outcome(ReconcilePreview(snapshot, plan, (), (), ()))
    human = render_human(outcome)
    raw = render_json(outcome)
    assert "credentials[*]" in human
    assert decode_receipt(raw).outcome == outcome
    for forbidden in (dynamic_key, *secrets):
        digest = hashlib.sha256(forbidden.encode()).hexdigest()
        assert forbidden not in human
        assert forbidden.encode() not in raw
        assert digest.encode() not in raw


def test_dynamic_cty_recursively_wildcards_unknown_map_and_sequence_identities():
    address = parse_opentofu_address("unifi_network.lan")
    outer_key = "tenant.one"
    inner_key = "credential_secret"
    secrets = ("deep-base", "deep-code", "deep-live")

    def values(secret):
        return _object({"payload": {outer_key: [{inner_key: [secret]}]}})

    base, desired, live = (values(secret) for secret in secrets)
    mask = _object({"payload": {outer_key: [{inner_key: [True]}]}})
    change = ResourceChange(
        address,
        ActionVector.UPDATE,
        live,
        desired,
        _object({}),
        before_sensitive=mask,
        after_sensitive=mask,
    )
    schema = ProviderSchema((('unifi_network', _object({"block": {"attributes": {
        "payload": {"type": "dynamic", "optional": True},
    }}})),))
    snapshot = normalize_reconcile_snapshot(
        plan=PlanDocument(
            (1, 0), StateDocument(((address, base),)), (change,), ((address, live),)
        ),
        schema=schema,
        live=ControllerProjection((), (), "b" * 64),
        module=_module(),
    )
    expected = ("payload", "*", "*", "*", "*")
    plan = build_reconcile_plan(snapshot)

    assert snapshot.resources[0].secret_changes[0].path == expected
    assert plan.decisions[0].conflict_paths == (expected,)
    outcome = reconcile_outcome(ReconcilePreview(snapshot, plan, (), (), ()))
    human = render_human(outcome)
    raw = render_json(outcome)
    assert "payload[*][*][*][*]" in human
    assert decode_receipt(raw).outcome == outcome
    for forbidden in (outer_key, inner_key, *secrets):
        digest = hashlib.sha256(forbidden.encode()).hexdigest()
        assert forbidden not in human
        assert forbidden.encode() not in raw
        assert digest.encode() not in raw


@pytest.mark.parametrize("cty_type", ["string", "number", "bool"])
def test_scalar_cty_rejects_nested_sensitive_mask_paths_before_public_outcomes(
    cty_type,
):
    address = parse_opentofu_address("unifi_network.lan")
    dynamic_key = "tenant.one"
    values = _object({"credentials": {dynamic_key: "synthetic-secret"}})
    mask = _object({"credentials": {dynamic_key: True}})
    change = ResourceChange(
        address,
        ActionVector.UPDATE,
        values,
        values,
        _object({}),
        before_sensitive=mask,
        after_sensitive=mask,
    )
    schema = ProviderSchema((('unifi_network', _object({"block": {"attributes": {
        "credentials": {"type": cty_type, "optional": True},
    }}})),))

    with pytest.raises(ExternalDocumentError) as exc_info:
        normalize_reconcile_snapshot(
            plan=PlanDocument(
                (1, 0),
                StateDocument(((address, values),)),
                (change,),
                ((address, values),),
            ),
            schema=schema,
            live=ControllerProjection((), (), "b" * 64),
            module=_module(),
        )

    assert (
        exc_info.value.kind,
        exc_info.value.field,
        exc_info.value.reason,
    ) == ("snapshot", "sensitive_values", "invalid document")
    assert dynamic_key not in str(exc_info.value)


def test_dotted_static_object_key_is_wildcarded_in_public_receipts():
    address = parse_opentofu_address("unifi_network.lan")
    static_key = "fixed.name"
    secrets = ("object-base", "object-code", "object-live")
    base = _object({"settings": {static_key: secrets[0]}})
    desired = _object({"settings": {static_key: secrets[1]}})
    live = _object({"settings": {static_key: secrets[2]}})
    mask = _object({"settings": {static_key: True}})
    change = ResourceChange(
        address,
        ActionVector.UPDATE,
        live,
        desired,
        _object({}),
        before_sensitive=mask,
        after_sensitive=mask,
    )
    schema = ProviderSchema((('unifi_network', _object({"block": {"attributes": {
        "settings": {
            "type": ["object", {static_key: "string"}],
            "optional": True,
        },
    }}})),))
    snapshot = normalize_reconcile_snapshot(
        plan=PlanDocument(
            (1, 0), StateDocument(((address, base),)), (change,), ((address, live),)
        ),
        schema=schema,
        live=ControllerProjection((), (), "b" * 64),
        module=_module(),
    )
    plan = build_reconcile_plan(snapshot)

    assert snapshot.resources[0].secret_changes[0].path == ("settings", "*")
    assert plan.decisions[0].conflict_paths == (("settings", "*"),)
    outcome = reconcile_outcome(ReconcilePreview(snapshot, plan, (), (), ()))
    human = render_human(outcome)
    raw = render_json(outcome)
    assert "settings[*]" in human
    assert decode_receipt(raw).outcome == outcome
    for forbidden in (static_key, *secrets):
        digest = hashlib.sha256(forbidden.encode()).hexdigest()
        assert forbidden not in human
        assert forbidden.encode() not in raw
        assert digest.encode() not in raw


def test_single_nested_block_projects_static_sensitive_attribute_path():
    address = parse_opentofu_address("unifi_network.lan")
    base = _object({"auth": {"token": "single-base"}})
    desired = _object({"auth": {"token": "single-code"}})
    live = _object({"auth": {"token": "single-live"}})
    change = ResourceChange(address, ActionVector.UPDATE, live, desired, _object({}))
    schema = ProviderSchema((('unifi_network', _object({"block": {
        "block_types": {"auth": {
            "nesting_mode": "single",
            "block": {"attributes": {
                "token": {"type": "string", "optional": True, "sensitive": True},
            }},
        }},
    }})),))

    snapshot = normalize_reconcile_snapshot(
        plan=PlanDocument(
            (1, 0), StateDocument(((address, base),)), (change,), ((address, live),)
        ),
        schema=schema,
        live=ControllerProjection((), (), "b" * 64),
        module=_module(),
    )

    assert snapshot.resources[0].secret_changes[0].path == ("auth", "token")
    assert build_reconcile_plan(snapshot).decisions[0].conflict_paths == (
        ("auth", "token"),
    )


def test_dynamic_cty_uses_observation_that_contains_path_amid_mixed_shapes():
    address = parse_opentofu_address("unifi_network.lan")
    dynamic_key = "tenant.one"
    base = _object({"credentials": "base-scalar"})
    desired = _object({"credentials": {dynamic_key: "synthetic-secret"}})
    live = _object({"credentials": "base-scalar"})
    mask = _object({"credentials": {dynamic_key: True}})
    change = ResourceChange(
        address,
        ActionVector.UPDATE,
        live,
        desired,
        _object({}),
        after_sensitive=mask,
    )
    schema = ProviderSchema((('unifi_network', _object({"block": {"attributes": {
        "credentials": {"type": "dynamic", "optional": True},
    }}})),))

    snapshot = normalize_reconcile_snapshot(
        plan=PlanDocument(
            (1, 0), StateDocument(((address, base),)), (change,), ((address, live),)
        ),
        schema=schema,
        live=ControllerProjection((), (), "b" * 64),
        module=_module(),
    )

    fact = snapshot.resources[0].secret_changes[0]
    assert fact.path == ("credentials", "*")
    assert fact.kind is SecretChangeKind.CODE_ONLY


def test_normalization_defensively_scrubs_fresh_projected_secret_values():
    plan, _, _ = _inputs()
    address = plan.changes[0].address
    schema = ProviderSchema((("unifi_network", _object({"block": {"attributes": {
        "name": {"type": "string", "optional": True},
        "vlan": {"type": "number", "optional": True},
        "credential": {"type": "string", "optional": True, "sensitive": True},
    }}})),))
    projection = ControllerProjection((ProjectedControllerResource(
        address,
        _object({
            "name": "lan",
            "vlan": 30,
            "credential": "synthetic-controller-secret",
        }),
        (("name",), ("vlan",)),
    ),), (), "safe-controller-digest")

    snapshot = normalize_reconcile_snapshot(
        plan=plan, schema=schema, live=projection, module=_module()
    )

    assert snapshot.resources[0].fresh == _object({"name": "lan", "vlan": 30})
    assert "synthetic-controller-secret" not in repr(snapshot)


def test_normalization_keeps_import_identity_separate_from_comparable_values():
    address = parse_opentofu_address("unifi_network.new")
    values = _object({"id": "synthetic-id", "name": "new"})
    plan = PlanDocument(
        (1, 0),
        StateDocument(()),
        (ResourceChange(address, ActionVector.NOOP, values, values, _object({})),),
        ((address, values),),
    )
    projection = ControllerProjection(
        (
            ProjectedControllerResource(
                address,
                _object({"name": "new"}),
                (("name",),),
                True,
                "synthetic-id",
            ),
        ),
        (),
        "digest",
    )

    snapshot = normalize_reconcile_snapshot(
        plan=plan,
        schema=ProviderSchema(()),
        live=projection,
        module=ModuleIndex((), (), (), (), ()),
    )

    assert snapshot.resources[0].desired == _object({"name": "new"})
    assert snapshot.resources[0].live == _object({"name": "new"})
    assert snapshot.resources[0].import_id == "synthetic-id"


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


def test_normalization_retains_fresh_presence_values_and_import_identity_separately():
    plan, schema, _ = _inputs()
    existing = plan.changes[0].address
    new_address = parse_opentofu_address("unifi_network.guest_wifi")
    projection = ControllerProjection(
        (
            ProjectedControllerResource(
                existing,
                _object({"name": "lan", "vlan": 30}),
                (("name",), ("vlan",)),
                True,
                "existing-id",
            ),
            ProjectedControllerResource(
                new_address,
                _object({"name": "guest"}),
                (("name",),),
                True,
                "new-id",
            ),
        ),
        (),
        "fresh-digest",
    )

    snapshot = normalize_reconcile_snapshot(
        plan=plan, schema=schema, live=projection, module=_module()
    )

    observations = {item.address: item for item in snapshot.resources}
    assert observations[existing].live == _object({"name": "lan", "vlan": 30})
    assert observations[existing].fresh_present is True
    assert observations[existing].fresh == _object({"name": "lan", "vlan": 30})
    fresh = observations[new_address]
    assert fresh.base is None and fresh.desired is None and fresh.live is None
    assert fresh.fresh_present is True
    assert fresh.fresh == _object({"name": "guest"})
    assert fresh.import_id == "new-id"


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


def test_normalization_keeps_current_and_deposed_instances_as_distinct_observations():
    plan, schema, live = _inputs()
    current = plan.changes[0].address
    deposed = parse_opentofu_address(current.absolute, deposed="deadbeef")
    deposed_values = _object({"name": "old", "vlan": 5})
    plan = PlanDocument(
        plan.format_version,
        StateDocument((*plan.prior_state.resources, (deposed, deposed_values))),
        (
            *plan.changes,
            ResourceChange(
                deposed,
                ActionVector.NOOP,
                deposed_values,
                deposed_values,
                _object({}),
            ),
        ),
        (*plan.plan_time_live, (deposed, deposed_values)),
    )

    snapshot = normalize_reconcile_snapshot(
        plan=plan, schema=schema, live=live, module=_module()
    )

    assert [(item.address.deposed, item.committed is not None) for item in snapshot.resources] == [
        (None, True),
        ("deadbeef", False),
    ]


def test_collector_uses_saved_plan_and_captures_exact_source_identity_without_state_query(
    monkeypatch, tmp_path
):
    source = b'resource "unifi_network" "lan" {\n  name = "lan"\n}\n'
    (tmp_path / "main.tf").write_bytes(source)
    module = index_effective_module(workdir=tmp_path)

    class Runner:
        workdir = tmp_path
        plan_path = tmp_path / ".ubitofu" / "tmp" / "session" / "tf.plan"

        def plan(self, *, out=None, generate_config_out=None):
            assert out == self.plan_path
            assert generate_config_out is None
            out.parent.mkdir(parents=True)
            out.write_bytes(b"private saved plan")
            out.chmod(0o600)
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
    new_record = ControllerRecord(
        "unifi_network",
        "new-id",
        _object({"_id": "new-id", "name": "guest"}),
    )
    enumeration = EnumerationResult(
        targets=[
            ImportTarget("unifi_network", "lan", "synthetic-id"),
            ImportTarget("unifi_network", "Guest WiFi", "new-id"),
        ],
        records=[record, new_record],
        covered_resource_types=["unifi_network"],
    )
    monkeypatch.setattr(
        "ubitofu.reconcile_snapshot.enumerate_controller",
        lambda controller, *, capture_records: enumeration,
    )

    snapshot = collect_reconcile_snapshot(
        controller=object(), runner=Runner(), module=module
    )
    assert Runner.plan_path.stat().st_mode & 0o777 == 0o600
    (tmp_path / "main.tf").write_bytes(b"changed after collection")

    observations = {item.address.absolute: item for item in snapshot.resources}
    committed = observations["unifi_network.lan"].committed
    assert committed is not None
    assert committed.block_bytes == source.rstrip(b"\n")
    assert committed.file.sha256 == hashlib.sha256(source).hexdigest()
    assert committed.file.size == len(source)
    fresh = observations["unifi_network.guest_wifi"]
    assert fresh.fresh_present is True
    assert fresh.fresh == _object({"name": "guest"})
    assert fresh.import_id == "new-id"


def test_collector_captures_exact_native_hcl_attribute_token_ownership(tmp_path):
    source = (
        b'resource "unifi_network" "lan" {\n'
        b'  name = "la\\u006e"\n'
        b'  vlan = var.vlan\n'
        b'}\n'
    )
    (tmp_path / "main.tf").write_bytes(source)
    module = index_effective_module(workdir=tmp_path)
    plan, schema, projection = _inputs()
    normalized = normalize_reconcile_snapshot(
        plan=plan, schema=schema, live=projection, module=module
    )

    captured = __import__(
        "ubitofu.reconcile_snapshot", fromlist=["_capture_source_files"]
    )._capture_source_files(normalized, tmp_path)

    committed = captured.resources[0].committed
    assert committed is not None
    assert [
        (attribute.attribute_path, attribute.expression_bytes, attribute.literal)
        for attribute in committed.source_attributes
    ] == [
        (("name",), b'"la\\u006e"', True),
        (("vlan",), b"var.vlan", False),
    ]
