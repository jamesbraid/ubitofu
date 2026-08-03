# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 James Braid
"""Pure resource-existence classification for reconcile."""

import pytest

from ubitofu.enumerator import ImportTarget
from ubitofu.pipeline import (
    ExistenceDecision,
    ExistenceFacts,
    _configured_addresses,
    _emitted_imports,
    _existence_facts,
    _plan_changes_by_address,
    _state_rows_by_address,
    classify_existence,
)


def _facts(**overrides):
    values = {
        "address": "unifi_network.example_net",
        "resource_type": "unifi_network",
        "config_present": False,
        "state_present": False,
    }
    values.update(overrides)
    return ExistenceFacts(**values)


@pytest.mark.parametrize("live_present", [False, True])
def test_missing_config_exact_delete_is_pending_destroy(live_present):
    facts = _facts(
        state_present=True,
        live_present=live_present,
        actions=("delete",),
        action_reason="delete_because_no_resource_config",
    )
    assert classify_existence(facts).kind is ExistenceDecision.PENDING_DESTROY


def test_exact_forget_is_pending_forget():
    facts = _facts(state_present=True, actions=("forget",))
    assert classify_existence(facts).kind is ExistenceDecision.PENDING_FORGET


@pytest.mark.parametrize("actions", [("delete", "create"), ("create", "delete")])
def test_replacement_requires_attention(actions):
    facts = _facts(config_present=True, state_present=True, actions=actions)
    assert classify_existence(facts).kind is ExistenceDecision.REPLACEMENT_ATTENTION


@pytest.mark.parametrize(
    "reason",
    [
        "delete_because_count_index",
        "delete_because_each_key",
        "delete_because_wrong_repetition",
        "delete_because_no_module",
        None,
    ],
)
def test_other_delete_reasons_require_attention(reason):
    facts = _facts(
        state_present=True,
        actions=("delete",),
        action_reason=reason,
    )
    assert classify_existence(facts).kind is ExistenceDecision.INVARIANT_ATTENTION


def test_configured_live_state_missing_imports_original_address():
    facts = _facts(
        config_present=True,
        live_present=True,
        live_identity="00112233445566778899aabb",
        actions=("create",),
    )
    decision = classify_existence(facts)
    assert decision.kind is ExistenceDecision.IMPORT_EXISTING_CONFIG
    assert decision.address == "unifi_network.example_net"
    assert decision.import_id == "00112233445566778899aabb"


def test_configured_creatable_state_missing_is_pending_create():
    facts = _facts(config_present=True, actions=("create",))
    assert classify_existence(facts).kind is ExistenceDecision.PENDING_CREATE


def test_expanded_config_controller_deletion_requires_attention():
    facts = _facts(
        address='unifi_network.example_net["example"]',
        config_present=True,
        state_present=True,
        actions=("create",),
        config_block_direct=False,
    )
    assert (
        classify_existence(facts).kind
        is ExistenceDecision.EXPANDED_DELETION_ATTENTION
    )


def test_configured_ui_only_state_missing_is_forbidden_create():
    facts = _facts(
        address="unifi_device.example_ap",
        resource_type="unifi_device",
        config_present=True,
        ui_lifecycle=True,
        actions=("create",),
    )
    assert classify_existence(facts).kind is ExistenceDecision.FORBIDDEN_CREATE


def test_ambiguous_identity_requires_attention():
    facts = _facts(
        config_present=True,
        actions=("create",),
        identity_joinable=False,
        live_type_present=True,
    )
    assert classify_existence(facts).kind is ExistenceDecision.IDENTITY_ATTENTION


def test_genuinely_new_live_object_is_appended():
    facts = _facts(
        address="unifi_network.example_new",
        live_present=True,
        live_identity="00112233445566778899aabb",
        scratch_import=True,
        actions=("create",),
    )
    assert classify_existence(facts).kind is ExistenceDecision.APPEND_NEW


def test_state_only_without_removal_transition_requires_attention():
    facts = _facts(state_present=True, live_present=True, actions=("update",))
    assert classify_existence(facts).kind is ExistenceDecision.INVARIANT_ATTENTION


def test_scratch_create_is_not_operator_pending_create():
    facts = _facts(
        address="unifi_network.example_scratch",
        live_present=True,
        live_identity="00112233445566778899aabb",
        scratch_import=True,
        actions=("create",),
    )
    assert classify_existence(facts).kind is ExistenceDecision.APPEND_NEW


def test_scratch_replacement_actions_are_not_operator_intent():
    facts = _facts(
        address="unifi_network.example_scratch",
        live_present=True,
        live_identity="00112233445566778899aabb",
        scratch_import=True,
        actions=("delete", "create"),
    )
    assert classify_existence(facts).kind is ExistenceDecision.APPEND_NEW


def test_unsafe_joined_live_identity_requires_attention():
    facts = _facts(
        config_present=True,
        live_present=True,
        live_identity="00112233445566778899aabb",
        identity_joinable=False,
    )
    assert classify_existence(facts).kind is ExistenceDecision.IDENTITY_ATTENTION


def test_existing_import_is_idempotent():
    facts = _facts(
        config_present=True,
        live_present=True,
        live_identity="00112233445566778899aabb",
        existing_import=True,
        actions=("create",),
    )
    assert classify_existence(facts).kind is ExistenceDecision.MANAGED


def test_identity_owned_by_another_state_address_is_not_imported():
    target = ImportTarget(
        "unifi_client", "example client", "00:11:22:00:00:03")
    configured = {"unifi_client.example_config"}
    state_rows = {
        "unifi_client.example_state": {
            "address": "unifi_client.example_state",
            "type": "unifi_client",
            "name": "example_state",
            "values": {"mac": "00:11:22:00:00:03"},
        }
    }
    plan_changes = {
        "unifi_client.example_config": {
            "address": "unifi_client.example_config",
            "type": "unifi_client",
            "name": "example_config",
            "change": {
                "actions": ["create"],
                "after": {"mac": "00:11:22:00:00:03"},
            },
        }
    }

    facts = _existence_facts(
        configured=configured,
        state_rows=state_rows,
        plan_changes=plan_changes,
        targets=[target],
        scratch_by_address={},
        emitted_imports={},
        site="default",
    )

    assert classify_existence(
        facts["unifi_client.example_config"]
    ).kind is ExistenceDecision.IDENTITY_ATTENTION


def test_two_config_addresses_cannot_claim_one_live_identity():
    target = ImportTarget(
        "unifi_client", "example client", "00:11:22:00:00:03")
    configured = {
        "unifi_client.example_one",
        "unifi_client.example_two",
    }
    plan_changes = {
        address: {
            "address": address,
            "type": "unifi_client",
            "name": address.rsplit(".", 1)[-1],
            "change": {
                "actions": ["create"],
                "after": {"mac": "00:11:22:00:00:03"},
            },
        }
        for address in configured
    }
    scratch_address = "unifi_client.example_client"

    facts = _existence_facts(
        configured=configured,
        state_rows={},
        plan_changes=plan_changes,
        targets=[target],
        scratch_by_address={scratch_address: target},
        emitted_imports={},
        site="default",
    )

    assert {
        classify_existence(facts[address]).kind for address in configured
    } == {ExistenceDecision.IDENTITY_ATTENTION}
    assert classify_existence(
        facts[scratch_address]
    ).kind is ExistenceDecision.MANAGED


def test_config_snapshot_counts_generated_resources_but_not_scaffolding(tmp_path):
    resource = 'resource "unifi_network" "{}" {{\n  name = "example"\n}}\n'
    for filename, slug in [
        ("operator.tf", "example_operator"),
        ("generated.tf", "example_bulk"),
        ("generated_new.tf", "example_incremental"),
        ("reconciled_new.tf", "example_reconciled"),
        ("generated_stub.tf", "example_stub"),
        ("ubitofu-reconcile-example.tf", "example_scratch"),
    ]:
        (tmp_path / filename).write_text(resource.format(slug))

    assert _configured_addresses(tmp_path) == {
        "unifi_network.example_operator",
        "unifi_network.example_bulk",
        "unifi_network.example_incremental",
        "unifi_network.example_reconciled",
    }


def test_state_snapshot_prefers_full_address_and_supports_legacy_rows():
    state = {"values": {"root_module": {"resources": [
        {
            "address": "module.example.unifi_network.example_full",
            "type": "unifi_network",
            "name": "example_full",
            "values": {"id": "00112233445566778899aabb"},
        },
        {
            "type": "unifi_network",
            "name": "example_legacy",
            "values": {"id": "aabbccddeeff001122334455"},
        },
    ]}}}
    assert set(_state_rows_by_address(state)) == {
        "module.example.unifi_network.example_full",
        "unifi_network.example_legacy",
    }


def test_plan_snapshot_prefers_full_address_and_supports_legacy_rows():
    plan = {"resource_changes": [
        {
            "address": "module.example.unifi_network.example_full",
            "type": "unifi_network",
            "name": "example_full",
            "change": {"actions": ["no-op"]},
        },
        {
            "type": "unifi_network",
            "name": "example_legacy",
            "change": {"actions": ["create"]},
        },
    ]}
    assert set(_plan_changes_by_address(plan)) == {
        "module.example.unifi_network.example_full",
        "unifi_network.example_legacy",
    }


@pytest.mark.parametrize("filename", ["imports.tf", "reconciled_new.tf"])
def test_existing_emitted_imports_are_keyed_by_original_address(tmp_path, filename):
    (tmp_path / filename).write_text(
        'resource "unifi_network" "example_net" {\n  name = "example"\n}\n\n'
        "import {\n"
        "  to = unifi_network.example_net\n"
        '  id = "00112233445566778899aabb"\n'
        "}\n"
    )
    assert _emitted_imports(tmp_path) == {
        "unifi_network.example_net": "00112233445566778899aabb"
    }
