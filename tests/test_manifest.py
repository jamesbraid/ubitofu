# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 James Braid
from ubitofu.manifest import (
    CLASSIFIED_SECTIONS,
    MANIFEST,
    PROBE_ENDPOINTS,
    UNMAPPED_ENDPOINTS,
    spec_for_type,
    specs_for_endpoint,
)


def test_manifest_has_27_resources():
    types = {s.resource_type for s in MANIFEST}
    assert len(types) == 27


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
    assert net.discriminator == {"purpose": "corporate|vlan-only"}


def test_mac_keyed_imports():
    assert spec_for_type("unifi_client").id_rule == "mac"
    assert spec_for_type("unifi_device").id_rule == "mac_or_id"
    assert spec_for_type("unifi_power_supervisor").id_rule == "mac"


def test_client_filter_is_fixed_ip_present():
    assert spec_for_type("unifi_client").include == {"fixed_ip": "__present__"}


def test_firewall_policy_filters_predefined_false():
    assert spec_for_type("unifi_firewall_policy").include == {"predefined": False}


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
    assert "unifi_ap_group" not in types


def test_unmapped_endpoints_flagged_not_mapped():
    assert "v2/api/site/{site}/nat" in UNMAPPED_ENDPOINTS
    assert "v2/api/site/{site}/content-filtering" in UNMAPPED_ENDPOINTS
    # AP groups exist on the controller but have no provider resource
    assert "v2/api/site/{site}/apgroups" in UNMAPPED_ENDPOINTS
    mapped = {s.endpoint for s in MANIFEST}
    assert "v2/api/site/{site}/nat" not in mapped
    assert "v2/api/site/{site}/apgroups" not in mapped


def test_probe_endpoints_cover_the_audited_collections():
    # The probe universe from the 2026-07-10 UDM audit. Endpoints later
    # claimed by a MANIFEST spec are skipped at runtime, so overlap with
    # MANIFEST is legal here — but today these must all be probe-only.
    for ep in (
        "v2/api/site/{site}/nat",
        "v2/api/site/{site}/content-filtering",
        "v2/api/site/{site}/apgroups",
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
