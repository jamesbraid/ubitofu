# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 James Braid
import json
import shutil
import subprocess
from pathlib import PurePosixPath

import pytest

from ubitofu.errors import ExternalDocumentError
from ubitofu.module_index import IndexedResource, IndexedSource, ModuleIndex
from ubitofu.reconcile_model import (
    ActionVector,
    ControllerProjection,
    Disposition,
    ProviderSchema,
    ReasonCode,
    parse_opentofu_address,
)
from ubitofu.reconcile_planner import build_reconcile_plan
from ubitofu.reconcile_snapshot import normalize_reconcile_snapshot
from ubitofu.tofu_json import (
    parse_plan_document,
    parse_provider_schema,
    parse_state_document,
    validate_document_header,
)
from ubitofu.values import FrozenObject, freeze_value


def _resource_change(
    *, actions, before, after, before_sensitive=None, after_sensitive=None
):
    return {
        "address": "unifi_network.lan",
        "mode": "managed",
        "type": "unifi_network",
        "name": "lan",
        "change": {
            "actions": actions,
            "before": before,
            "after": after,
            "after_unknown": {},
            "before_sensitive": {} if before_sensitive is None else before_sensitive,
            "after_sensitive": {} if after_sensitive is None else after_sensitive,
        },
    }


def _state_document_with_resource(row):
    return {
        "format_version": "1.0",
        "values": {"root_module": {"resources": [row]}},
    }


@pytest.mark.parametrize("kind", ["plan", "state"])
def test_external_documents_reject_malformed_absolute_resource_addresses(kind):
    row = {
        "address": "not an address",
        "mode": "managed",
        "type": "unifi_network",
        "name": "lan",
        "values": {"name": "lan"},
    }
    if kind == "plan":
        row["change"] = {
            "actions": ["create"],
            "before": None,
            "after": {"name": "lan"},
            "after_unknown": {},
        }
        document = {
            "format_version": "1.0",
            "errored": False,
            "resource_changes": [row],
        }
        parse = parse_plan_document
    else:
        document = _state_document_with_resource(row)
        parse = parse_state_document

    with pytest.raises(ExternalDocumentError) as exc_info:
        parse(document)

    assert (exc_info.value.kind, exc_info.value.field, exc_info.value.reason) == (
        kind,
        "address",
        "invalid document",
    )
    assert "not an address" not in str(exc_info.value)


@pytest.mark.parametrize(
    ("field", "mismatch"),
    [
        ("mode", "data"),
        ("type", "unifi_wlan"),
        ("name", "guest"),
        ("module_address", "module.other"),
        ("index", "green"),
    ],
)
def test_plan_rejects_metadata_that_disagrees_with_legal_absolute_address(
    field, mismatch
):
    row = {
        "address": 'module.edge.unifi_network.lan["blue"]',
        "module_address": "module.edge",
        "mode": "managed",
        "type": "unifi_network",
        "name": "lan",
        "index": "blue",
        "change": {
            "actions": ["create"],
            "before": None,
            "after": {"name": "lan"},
            "after_unknown": {},
        },
    }
    row[field] = mismatch

    with pytest.raises(ExternalDocumentError) as exc_info:
        parse_plan_document(
            {
                "format_version": "1.0",
                "errored": False,
                "resource_changes": [row],
            }
        )

    assert (exc_info.value.kind, exc_info.value.field, exc_info.value.reason) == (
        "plan",
        "address",
        "invalid document",
    )


@pytest.mark.parametrize(
    ("value", "kind", "expected"),
    [
        ({"format_version": "1.0", "errored": False, "future": {"kept": True}}, "plan", (1, 0)),
        ({"format_version": "1.9"}, "state", (1, 9)),
        ({"format_version": "1.2"}, "provider_schema", (1, 2)),
    ],
)
def test_validate_document_header_accepts_supported_major_and_preserves_minor_fields(
    value, kind, expected
):
    header, document = validate_document_header(value, kind=kind)

    assert header.format_version == expected
    assert document is value


@pytest.mark.parametrize(
    ("value", "kind"),
    [
        ([], "plan"),
        ({}, "plan"),
        ({"format_version": 1, "errored": False}, "plan"),
        ({"format_version": "2.0", "errored": False}, "plan"),
        ({"format_version": "1.0"}, "plan"),
        ({"format_version": "1.0", "errored": "false"}, "plan"),
        ({"format_version": "1.0", "errored": True}, "plan"),
    ],
)
def test_validate_document_header_rejects_malformed_or_errored_documents(value, kind):
    with pytest.raises(ExternalDocumentError):
        validate_document_header(value, kind=kind)


def test_parse_plan_retains_only_used_saved_plan_fields_and_deep_copies_input():
    raw = {
        "format_version": "1.99",
        "errored": False,
        "prior_state": {
            "format_version": "1.0",
            "values": {
                "root_module": {
                    "resources": [
                        {
                            "address": "unifi_network.lan",
                            "mode": "managed",
                            "type": "unifi_network",
                            "name": "lan",
                            "values": {"name": "lan", "vlan": 10},
                            "provider_name": "ignored-minor-field",
                        }
                    ]
                }
            },
        },
        "resource_changes": [
            {
                "address": "unifi_network.lan",
                "mode": "managed",
                "type": "unifi_network",
                "name": "lan",
                "change": {
                    "actions": ["update"],
                    "before": {"name": "lan", "vlan": 11},
                    "after": {"name": "lan", "vlan": 10},
                    "after_unknown": {"computed": True},
                    "ignored": "minor extension",
                },
            }
        ],
        "resource_drift": [
            {
                "address": "unifi_network.lan",
                "mode": "managed",
                "type": "unifi_network",
                "name": "lan",
                "change": {
                    "actions": ["update"],
                    "before": {"name": "lan", "vlan": 9},
                    "after": {"name": "lan", "vlan": 11},
                    "after_unknown": {},
                },
            }
        ],
        "unknown_minor_field": {"do": "not retain"},
    }

    parsed = parse_plan_document(raw)
    raw["resource_changes"][0]["change"]["before"]["vlan"] = 999

    assert parsed.format_version == (1, 99)
    assert parsed.prior_state.resources[0][1] == FrozenObject(
        (("name", "lan"), ("vlan", 10))
    )
    assert parsed.changes[0].before == FrozenObject((("name", "lan"), ("vlan", 11)))
    assert parsed.changes[0].after_unknown == FrozenObject((("computed", True),))
    assert parsed.plan_time_live[0][1] == parsed.changes[0].before


def test_parse_plan_retains_sensitive_masks_without_exposing_values_through_them():
    raw = {
        "format_version": "1.0",
        "errored": False,
        "prior_state": {"format_version": "1.0", "values": {}},
        "resource_changes": [{
            "address": "unifi_wlan.wifi",
            "mode": "managed",
            "type": "unifi_wlan",
            "name": "wifi",
            "change": {
                "actions": ["update"],
                "before": {"passphrase": "synthetic-before"},
                "after": {"passphrase": "synthetic-after"},
                "after_unknown": {},
                "before_sensitive": {"passphrase": True},
                "after_sensitive": {"passphrase": True},
            },
        }],
    }

    parsed = parse_plan_document(raw)

    assert parsed.changes[0].before_sensitive == FrozenObject((("passphrase", True),))
    assert parsed.changes[0].after_sensitive == FrozenObject((("passphrase", True),))
    assert "synthetic" not in repr(parsed.changes[0].before_sensitive)


def test_parse_plan_accepts_nonsensitive_collection_elements_in_mask():
    raw = {
        "format_version": "1.0",
        "errored": False,
        "prior_state": {"format_version": "1.0", "values": {}},
        "resource_changes": [
            _resource_change(
                actions=["no-op"],
                before={"device_macs": ["aa:bb"], "passphrase": "synthetic-secret"},
                after={"device_macs": ["aa:bb"], "passphrase": "synthetic-secret"},
                before_sensitive={"device_macs": [False], "passphrase": True},
                after_sensitive={"device_macs": [False], "passphrase": True},
            )
        ],
    }

    parsed = parse_plan_document(raw)

    assert parsed.changes[0].before_sensitive == freeze_value(
        {"device_macs": [False], "passphrase": True}
    )
    assert parsed.changes[0].after_sensitive == parsed.changes[0].before_sensitive


def test_parse_fresh_create_accepts_omitted_prior_state_and_root_false_mask():
    raw = {
        "format_version": "1.2",
        "terraform_version": "1.12.0",
        "errored": False,
        "resource_changes": [
            _resource_change(
                actions=["create"],
                before=None,
                after={"name": "lan"},
                before_sensitive=False,
            )
        ],
    }

    parsed = parse_plan_document(raw)

    assert parsed.prior_state.resources == ()
    assert parsed.changes[0].before_sensitive == FrozenObject(())
    assert parsed.changes[0].after_sensitive == FrozenObject(())


def test_parse_delete_accepts_root_false_after_sensitive_mask():
    raw = {
        "format_version": "1.2",
        "terraform_version": "1.12.0",
        "errored": False,
        "prior_state": {
            "format_version": "1.0",
            "values": {"root_module": {"resources": [{
                "address": "unifi_network.lan",
                "mode": "managed",
                "type": "unifi_network",
                "name": "lan",
                "values": {"name": "lan"},
                "sensitive_values": {},
            }]}},
        },
        "resource_changes": [
            _resource_change(
                actions=["delete"],
                before={"name": "lan"},
                after=None,
                after_sensitive=False,
            )
        ],
    }

    parsed = parse_plan_document(raw)

    assert parsed.changes[0].before_sensitive == FrozenObject(())
    assert parsed.changes[0].after_sensitive == FrozenObject(())


@pytest.mark.parametrize(
    ("actions", "before", "after"),
    [
        (["update"], {"name": "old"}, {"name": "new"}),
        (["delete"], {"name": "old"}, None),
        (["create"], {"name": "impossible"}, {"name": "new"}),
    ],
)
def test_plan_without_prior_state_rejects_non_fresh_create_changes(
    actions, before, after
):
    raw = {
        "format_version": "1.2",
        "errored": False,
        "resource_changes": [
            _resource_change(actions=actions, before=before, after=after)
        ],
    }

    with pytest.raises(ExternalDocumentError, match="prior_state"):
        parse_plan_document(raw)


@pytest.mark.parametrize("prior_state", [None, [], False])
def test_plan_rejects_present_malformed_prior_state(prior_state):
    raw = {
        "format_version": "1.2",
        "errored": False,
        "prior_state": prior_state,
        "resource_changes": [],
    }

    with pytest.raises(ExternalDocumentError, match="prior_state"):
        parse_plan_document(raw)


def test_parsed_create_and_delete_reach_planner_as_typed_pending_decisions():
    create = parse_plan_document({
        "format_version": "1.2",
        "errored": False,
        "resource_changes": [
            _resource_change(
                actions=["create"],
                before=None,
                after={"name": "lan"},
                before_sensitive=False,
            )
        ],
    })
    delete = parse_plan_document({
        "format_version": "1.2",
        "errored": False,
        "prior_state": {
            "format_version": "1.0",
            "values": {"root_module": {"resources": [{
                "address": "unifi_network.lan",
                "mode": "managed",
                "type": "unifi_network",
                "name": "lan",
                "values": {"name": "lan"},
            }]}},
        },
        "resource_changes": [
            _resource_change(
                actions=["delete"],
                before={"name": "lan"},
                after=None,
                after_sensitive=False,
            )
        ],
    })
    source = IndexedSource(PurePosixPath("main.tf"), "native", True, None, None)
    create_module = ModuleIndex(
        (source,),
        (IndexedResource("unifi_network.lan", source.relative_path, None, True),),
        (),
        (),
        (),
    )

    create_decision = build_reconcile_plan(normalize_reconcile_snapshot(
        plan=create,
        schema=ProviderSchema(()),
        live=ControllerProjection((), (), "create-digest"),
        module=create_module,
    )).decisions[0]
    delete_decision = build_reconcile_plan(normalize_reconcile_snapshot(
        plan=delete,
        schema=ProviderSchema(()),
        live=ControllerProjection((), (), "delete-digest"),
        module=ModuleIndex((), (), (), (), ()),
    )).decisions[0]

    assert (create_decision.disposition, create_decision.reason) == (
        Disposition.PRESERVE_CODE,
        ReasonCode.PENDING_CREATE,
    )
    assert (delete_decision.disposition, delete_decision.reason) == (
        Disposition.PRESERVE_CODE,
        ReasonCode.PENDING_DELETE,
    )


@pytest.mark.skipif(shutil.which("tofu") is None, reason="OpenTofu is not installed")
def test_installed_opentofu_create_and_delete_documents_match_parser_contract(tmp_path):
    config = tmp_path / "main.tf"
    config.write_text(
        'terraform { required_version = ">= 1.12.0" }\n'
        'resource "terraform_data" "contract" { input = "create" }\n'
    )

    def run(*arguments):
        return subprocess.run(
            ("tofu", *arguments),
            cwd=tmp_path,
            check=True,
            capture_output=True,
            text=True,
        ).stdout

    run("init", "-backend=false", "-input=false")
    run("plan", "-input=false", "-out=create.tfplan")
    create_raw = json.loads(run("show", "-json", "create.tfplan"))
    assert create_raw["terraform_version"].startswith("1.12.")
    assert create_raw["resource_changes"][0]["change"]["before_sensitive"] is False

    create = parse_plan_document(create_raw)
    assert create.prior_state.resources == ()
    assert create.changes[0].action is ActionVector.CREATE
    assert create.changes[0].before_sensitive == FrozenObject(())

    run("apply", "-input=false", "-auto-approve", "create.tfplan")
    config.write_text('terraform { required_version = ">= 1.12.0" }\n')
    run("plan", "-input=false", "-out=delete.tfplan")
    delete_raw = json.loads(run("show", "-json", "delete.tfplan"))
    assert delete_raw["resource_changes"][0]["change"]["after_sensitive"] is False

    delete = parse_plan_document(delete_raw)
    assert delete.changes[0].action is ActionVector.DELETE
    assert delete.changes[0].after_sensitive == FrozenObject(())


def test_parse_state_and_provider_schema_copy_nested_external_values():
    state_raw = {
        "format_version": "1.0",
        "values": {"root_module": {"resources": [{
            "address": 'unifi_network.lan["blue"]',
            "mode": "managed",
            "type": "unifi_network",
            "name": "lan",
            "index": "blue",
            "values": {"nested": [{"enabled": True}]},
        }]}},
    }
    schema_raw = {
        "format_version": "1.0",
        "provider_schemas": {
            "synthetic/provider": {
                "resource_schemas": {
                    "unifi_network": {
                        "block": {
                            "attributes": {"name": {"required": True, "type": "string"}}
                        }
                    }
                }
            }
        },
    }

    state = parse_state_document(state_raw)
    schema = parse_provider_schema(schema_raw)
    state_raw["values"]["root_module"]["resources"][0]["values"]["nested"][0][
        "enabled"
    ] = False

    assert state.resources[0][0].index == "blue"
    assert state.resources[0][1] == FrozenObject(
        (("nested", (FrozenObject((("enabled", True),)),)),)
    )
    assert schema.resources[0][0] == "unifi_network"


def test_parse_state_retains_value_free_sensitivity_masks():
    state = parse_state_document({
        "format_version": "1.0",
        "values": {"root_module": {"resources": [{
            "address": "unifi_wlan.wifi",
            "mode": "managed",
            "type": "unifi_wlan",
            "name": "wifi",
            "values": {"name": "wifi", "passphrase": "synthetic-secret"},
            "sensitive_values": {"passphrase": True},
        }]}},
    })

    assert state.sensitive_values == ((
        parse_opentofu_address("unifi_wlan.wifi"),
        FrozenObject((("passphrase", True),)),
    ),)
    assert "synthetic-secret" not in repr(state.sensitive_values)


@pytest.mark.parametrize(
    "invalid_mask",
    ["yes", {"passphrase": "yes"}, {"nested": [None]}],
)
def test_plan_rejects_invalid_sensitive_masks(invalid_mask):
    raw = {
        "format_version": "1.0",
        "errored": False,
        "prior_state": {"format_version": "1.0", "values": {}},
        "resource_changes": [{
            "address": "unifi_wlan.wifi",
            "mode": "managed",
            "type": "unifi_wlan",
            "name": "wifi",
            "change": {
                "actions": ["no-op"],
                "before": {"passphrase": "synthetic-secret"},
                "after": {"passphrase": "synthetic-secret"},
                "after_unknown": {},
                "before_sensitive": invalid_mask,
                "after_sensitive": {"passphrase": True},
            },
        }],
    }

    with pytest.raises(ExternalDocumentError):
        parse_plan_document(raw)


def test_state_rejects_invalid_sensitive_mask():
    raw = {
        "format_version": "1.0",
        "values": {"root_module": {"resources": [{
            "address": "unifi_wlan.wifi",
            "mode": "managed",
            "type": "unifi_wlan",
            "name": "wifi",
            "values": {"name": "wifi", "passphrase": "synthetic-secret"},
            "sensitive_values": {"passphrase": "false"},
        }]}},
    }

    with pytest.raises(ExternalDocumentError):
        parse_state_document(raw)


def test_plan_and_state_keep_current_and_deposed_instances_with_same_absolute_address():
    rows = [
        {
            "address": "unifi_network.lan",
            "mode": "managed",
            "type": "unifi_network",
            "name": "lan",
            "values": {"name": "current"},
        },
        {
            "address": "unifi_network.lan",
            "mode": "managed",
            "type": "unifi_network",
            "name": "lan",
            "deposed": "deadbeef",
            "values": {"name": "deposed"},
        },
    ]
    state_raw = {
        "format_version": "1.0",
        "values": {"root_module": {"resources": rows}},
    }
    changes = [
        {
            **{key: value for key, value in row.items() if key != "values"},
            "change": {
                "actions": ["no-op"],
                "before": row["values"],
                "after": row["values"],
                "after_unknown": {},
            },
        }
        for row in rows
    ]
    plan_raw = {
        "format_version": "1.0",
        "errored": False,
        "prior_state": state_raw,
        "resource_changes": changes,
    }

    state = parse_state_document(state_raw)
    plan = parse_plan_document(plan_raw)

    decoded_state = [
        (address.deposed, dict(values.items)["name"])
        for address, values in state.resources
    ]
    assert decoded_state == [
        (None, "current"),
        ("deadbeef", "deposed"),
    ]
    assert [(change.address.deposed, change.before) for change in plan.changes] == [
        (None, FrozenObject((("name", "current"),))),
        ("deadbeef", FrozenObject((("name", "deposed"),))),
    ]


@pytest.mark.parametrize(
    "mutator",
    [
        lambda raw: raw["resource_changes"][0]["change"].update(actions=["move"]),
        lambda raw: raw["resource_changes"][0]["change"].update(before=[]),
        lambda raw: raw["resource_changes"][0].pop("address"),
    ],
)
def test_parse_plan_rejects_malformed_used_fields(mutator):
    raw = {
        "format_version": "1.0",
        "errored": False,
        "prior_state": {"format_version": "1.0", "values": {"root_module": {}}},
        "resource_changes": [{
            "address": "unifi_network.lan",
            "mode": "managed",
            "type": "unifi_network",
            "name": "lan",
            "change": {"actions": ["no-op"], "before": {}, "after": {},
                       "after_unknown": {}},
        }],
    }
    mutator(raw)

    with pytest.raises(ExternalDocumentError):
        parse_plan_document(raw)
