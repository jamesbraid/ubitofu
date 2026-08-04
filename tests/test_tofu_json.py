# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 James Braid
import pytest

from ubitofu.errors import ExternalDocumentError
from ubitofu.tofu_json import (
    parse_plan_document,
    parse_provider_schema,
    parse_state_document,
    validate_document_header,
)
from ubitofu.values import FrozenObject


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
