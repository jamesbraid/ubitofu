from dataclasses import FrozenInstanceError

import pytest

from ubitofu.reconcile_model import (
    ActionVector,
    AppendImport,
    Disposition,
    OpenTofuAddress,
    ReasonCode,
    ReconcilePlan,
    ResourceDecision,
    parse_opentofu_address,
)


@pytest.mark.parametrize(
    ("absolute", "module", "mode", "resource_type", "name", "index"),
    [
        ("unifi_network.lan", None, "managed", "unifi_network", "lan", None),
        (
            "module.edge.module.site.unifi_network.lan[2]",
            "module.edge.module.site",
            "managed",
            "unifi_network",
            "lan",
            2,
        ),
        (
            'module.edge.data.unifi_site.current["west"]',
            "module.edge",
            "data",
            "unifi_site",
            "current",
            "west",
        ),
    ],
)
def test_address_parser_retains_complete_opaque_address_and_supported_identity(
    absolute, module, mode, resource_type, name, index
):
    address = parse_opentofu_address(absolute)

    assert address == OpenTofuAddress(
        absolute=absolute,
        module=module,
        mode=mode,
        resource_type=resource_type,
        name=name,
        index=index,
        deposed=None,
    )


def test_address_can_retain_deposed_identity_without_rewriting_absolute_address():
    address = parse_opentofu_address("unifi_network.lan", deposed="old-generation")

    assert address.absolute == "unifi_network.lan"
    assert address.deposed == "old-generation"


def test_legal_unsupported_address_is_retained_for_planner_attention():
    address = parse_opentofu_address("module.edge[0].unifi_network.lan")

    assert address.absolute == "module.edge[0].unifi_network.lan"
    assert address.module == "module.edge[0]"


def test_reconcile_plan_derives_blocking_and_hides_every_edit_when_blocked():
    first = parse_opentofu_address("unifi_network.a")
    second = parse_opentofu_address("unifi_network.b")
    append = AppendImport(first, "synthetic-a")
    plan = ReconcilePlan(
        decisions=(
            ResourceDecision(
                first,
                Disposition.APPEND,
                ReasonCode.LIVE_RESOURCE_NEW,
                (append,),
                (),
            ),
            ResourceDecision(
                second,
                Disposition.ATTENTION,
                ReasonCode.UNSUPPORTED_ADDRESS,
                (),
                (),
            ),
        )
    )

    assert plan.blocked is True
    assert plan.edits == ()


def test_reconcile_plan_sorts_flattened_edits_by_address():
    later = parse_opentofu_address("unifi_network.z")
    earlier = parse_opentofu_address("unifi_network.a")
    plan = ReconcilePlan(
        decisions=(
            ResourceDecision(
                later,
                Disposition.APPEND,
                ReasonCode.LIVE_RESOURCE_NEW,
                (AppendImport(later, "synthetic-z"),),
                (),
            ),
            ResourceDecision(
                earlier,
                Disposition.APPEND,
                ReasonCode.LIVE_RESOURCE_NEW,
                (AppendImport(earlier, "synthetic-a"),),
                (),
            ),
        )
    )

    assert [edit.address.absolute for edit in plan.edits] == [
        "unifi_network.a",
        "unifi_network.z",
    ]


def test_reconcile_models_are_frozen():
    decision = ResourceDecision(
        parse_opentofu_address("unifi_network.lan"),
        Disposition.NO_CHANGE,
        ReasonCode.NO_CHANGE,
        (),
        (),
    )

    with pytest.raises(FrozenInstanceError):
        decision.messages = ("changed",)


def test_all_documented_action_vectors_have_exact_wire_values():
    assert {item.value for item in ActionVector} == {
        ("no-op",),
        ("create",),
        ("read",),
        ("update",),
        ("delete",),
        ("forget",),
        ("delete", "create"),
        ("create", "delete"),
    }
