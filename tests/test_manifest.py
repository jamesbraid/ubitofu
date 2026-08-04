# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 James Braid
import pytest

from ubitofu.manifest import (
    CLASSIFIED_SECTIONS,
    MANIFEST,
    PROBE_ENDPOINTS,
    ResourceSpec,
    spec_for_type,
    specs_for_endpoint,
    validate_manifest,
)
from ubitofu.reconcile_model import (
    CollectionIdentityPolicy,
    ControllerFieldPolicy,
    GenerationNormalizationPolicy,
    LifecyclePolicy,
)
from ubitofu.values import FrozenObject


def test_manifest_has_28_resources():
    types = {s.resource_type for s in MANIFEST}
    assert len(types) == 28


def test_networkconf_is_discriminated_into_five_resources():
    specs = specs_for_endpoint("rest/networkconf")
    by_type = {s.resource_type for s in specs}
    assert by_type == {
        "unifi_network",
        "unifi_wan",
        "unifi_vpn_server",
        "unifi_vpn_client",
        "unifi_site_to_site_vpn",
    }
    net = spec_for_type("unifi_network")
    assert net.discriminator == FrozenObject((("purpose", "corporate|vlan-only"),))


def test_mac_keyed_imports():
    assert spec_for_type("unifi_client").id_rule == "mac"
    assert spec_for_type("unifi_device").id_rule == "mac_or_id"


def test_power_supervisor_is_id_keyed_not_mac_keyed():
    # The v2 record has no `mac` key — the device MAC is `client_mac` — so a "mac"
    # rule derives None and aborts the run. Identity is the controller `id`, which
    # is also what the provider's identity schema requires for import.
    assert spec_for_type("unifi_power_supervisor").id_rule == "_id"


def test_client_filter_is_fixed_ip_present():
    assert spec_for_type("unifi_client").include == FrozenObject(
        (("fixed_ip", "__present__"),)
    )


def test_firewall_policy_filters_predefined_false():
    assert spec_for_type("unifi_firewall_policy").include == FrozenObject(
        (("predefined", False),)
    )


def test_singletons_import_by_site():
    assert spec_for_type("unifi_setting").id_rule == "site"
    assert spec_for_type("unifi_bgp").id_rule == "site"
    # bgp lives at its own v2 endpoint, NOT rest/routing (that is static routes)
    assert spec_for_type("unifi_bgp").endpoint == "v2/api/site/{site}/bgp/config"


def test_wireguard_peer_two_level():
    assert spec_for_type("unifi_wireguard_peer").id_rule == "wg_two_level"


def test_v053_resource_set_matches_provider():
    # The two real v0.53 types the draft had wrong: usergroup backs
    # client_qos_rate (ClientGroup), routing backs static_route.
    assert spec_for_type("unifi_client_qos_rate").endpoint == "rest/usergroup"
    assert spec_for_type("unifi_static_route").endpoint == "rest/routing"
    types = {s.resource_type for s in MANIFEST}
    # phantom types removed (never existed in the provider)
    assert "unifi_user_group" not in types


def test_ap_group_is_mapped_by_id_on_the_v2_endpoint():
    spec = spec_for_type("unifi_ap_group")
    assert spec.endpoint == "v2/api/site/{site}/apgroups"
    assert spec.id_rule == "_id"
    # the built-in "All APs" default is filtered by the enumerator, not a
    # discriminator, so the spec itself carries no include/discriminator
    assert spec.discriminator is None and spec.include is None


def test_probe_endpoints_cover_the_audited_collections():
    # The probe universe from the 2026-07-10 UDM audit. Endpoints later
    # claimed by a MANIFEST spec are skipped at runtime, so overlap with
    # MANIFEST is legal here — but today these must all be probe-only.
    for ep in (
        "v2/api/site/{site}/nat",
        "v2/api/site/{site}/content-filtering",
        "v2/api/site/{site}/trafficrules",
        "v2/api/site/{site}/qos-rules",
        "v2/api/site/{site}/acl-rules",
        "v2/api/site/{site}/wan-slas",
        "v2/api/site/{site}/device-tags",
        "rest/scheduletask",
        "rest/dpigroup",
        "rest/dpiapp",
        "rest/wlangroup",
        "rest/hotspotop",
        "rest/hotspotpackage",
        "rest/hotspot2conf",
        "rest/channelplan",
    ):
        assert ep in PROBE_ENDPOINTS
        assert PROBE_ENDPOINTS[ep]  # non-empty label


def test_probe_endpoints_are_currently_unmapped():
    mapped = {s.endpoint for s in MANIFEST}
    assert not mapped & set(PROBE_ENDPOINTS)


def test_classified_sections_is_deliberately_tiny():
    # Acceptance lives in git (COVERAGE.md merges), not in code. Only
    # structurally-unclosable entries belong here. Growing this dict is a
    # design decision — the test forces that conversation.
    assert set(CLASSIFIED_SECTIONS) == {"super_*"}


def test_unifi_device_is_ui_lifecycle():
    # Devices are adopted/removed only in the UI; tofu must never create one.
    assert spec_for_type("unifi_device").ui_lifecycle is True


def test_ui_lifecycle_defaults_false():
    assert spec_for_type("unifi_network").ui_lifecycle is False
    assert spec_for_type("unifi_client").ui_lifecycle is False


def test_manifest_owns_complete_lifecycle_and_generation_normalization_policies():
    device = spec_for_type("unifi_device")
    port_forward = spec_for_type("unifi_port_forward")
    network = spec_for_type("unifi_network")

    assert device.lifecycle == LifecyclePolicy(True, "capture")
    assert network.lifecycle == LifecyclePolicy(False, "attention")
    assert port_forward.generation_normalization == GenerationNormalizationPolicy(
        "port_forward_wan_all_to_both"
    )
    assert network.generation_normalization == GenerationNormalizationPolicy("identity")


def test_manifest_owns_stable_collection_identity_policy():
    device = spec_for_type("unifi_device")

    assert device.collection_identities == (
        CollectionIdentityPolicy(("port_override",), "port_idx"),
    )


def test_resource_spec_deep_freezes_mutable_policies_at_construction():
    discriminator = {"purpose": "synthetic"}
    include = {"nested": {"enabled": True}}
    spec = ResourceSpec(
        "unifi_synthetic",
        "rest/synthetic",
        "_id",
        discriminator=discriminator,
        include=include,
    )
    discriminator["purpose"] = "changed"
    include["nested"]["enabled"] = False

    assert spec.discriminator == FrozenObject((("purpose", "synthetic"),))
    assert spec.include == FrozenObject(
        (("nested", FrozenObject((("enabled", True),))),)
    )


@pytest.mark.parametrize(
    "spec",
    [
        ResourceSpec("unifi_one", "rest/one", "_id"),
        ResourceSpec("unifi_two", "rest/two", "site"),
    ],
)
def test_complete_public_manifest_has_valid_typed_policy_tuples(spec):
    assert validate_manifest((*MANIFEST, spec))[-1] == spec


@pytest.mark.parametrize(
    "build_specs",
    [
        lambda: (
            ResourceSpec("unifi_duplicate", "rest/one", "_id"),
            ResourceSpec("unifi_duplicate", "rest/two", "_id"),
        ),
        lambda: (
            ResourceSpec(
                "unifi_collection",
                "rest/collection",
                "_id",
                collection_identities=(
                    CollectionIdentityPolicy(("items",), "id"),
                    CollectionIdentityPolicy(("items",), "name"),
                ),
            ),
        ),
        lambda: (
            ResourceSpec(
                "unifi_projection",
                "rest/projection",
                "_id",
                controller_fields=(
                    ControllerFieldPolicy(("enabled",), ("enabled",), "bool"),
                    ControllerFieldPolicy(("active",), ("enabled",), "bool"),
                ),
            ),
        ),
    ],
)
def test_manifest_rejects_duplicate_resource_and_policy_paths(build_specs):
    with pytest.raises(ValueError):
        validate_manifest(build_specs())


def test_resource_spec_rejects_malformed_policy_entries_before_use():
    with pytest.raises(ValueError):
        ResourceSpec(
            "unifi_bad",
            "rest/bad",
            "_id",
            collection_identities=(CollectionIdentityPolicy((), ""),),
        )

    with pytest.raises(ValueError):
        ResourceSpec(
            "unifi_bad",
            "rest/bad",
            "_id",
            lifecycle=LifecyclePolicy(False, "invalid"),  # type: ignore[arg-type]
        )

    with pytest.raises(ValueError):
        ResourceSpec(
            "unifi_bad",
            "rest/bad",
            "_id",
            generation_normalization=GenerationNormalizationPolicy("invalid"),  # type: ignore[arg-type]
        )
