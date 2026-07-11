# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 James Braid
from dataclasses import dataclass


@dataclass(frozen=True)
class ResourceSpec:
    resource_type: str
    endpoint: str
    id_rule: str  # "_id" | "site:_id" | "mac" | "mac_or_id" | "site" | "wg_two_level"
    site_scoped: bool = True
    discriminator: dict[str, str] | None = None
    include: dict[str, object] | None = None
    # Singleton (id_rule="site") to skip when its config endpoint is empty:
    # the by-site import fails when no remote object exists (e.g. BGP unset).
    skip_if_empty: bool = False


MANIFEST: tuple[ResourceSpec, ...] = (
    # rest/networkconf — one endpoint, five resources, discriminated on purpose
    ResourceSpec("unifi_network", "rest/networkconf", "_id",
                 discriminator={"purpose": "corporate|vlan-only"}),
    ResourceSpec("unifi_wan", "rest/networkconf", "_id",
                 discriminator={"purpose": "wan"}),
    ResourceSpec("unifi_vpn_server", "rest/networkconf", "_id",
                 discriminator={"purpose": "remote-user-vpn"}),
    ResourceSpec("unifi_vpn_client", "rest/networkconf", "_id",
                 discriminator={"purpose": "vpn-client"}),
    ResourceSpec("unifi_site_to_site_vpn", "rest/networkconf", "_id",
                 discriminator={"purpose": "site-vpn", "vpn_type": "ipsec"}),
    # MAC-keyed
    ResourceSpec("unifi_client", "rest/user", "mac",
                 include={"fixed_ip": "__present__"}),
    ResourceSpec("unifi_device", "stat/device", "mac_or_id"),
    ResourceSpec("unifi_power_supervisor",
                 "v2/api/site/{site}/power-supervisors", "mac"),
    # two-level
    ResourceSpec("unifi_wireguard_peer",
                 "v2/api/site/{site}/wireguard", "wg_two_level"),
    # singletons (import by site name — provider sets id = site on read)
    ResourceSpec("unifi_setting", "get/setting", "site"),
    ResourceSpec("unifi_bgp", "v2/api/site/{site}/bgp/config", "site",
                 skip_if_empty=True),
    # global / special
    ResourceSpec("unifi_site", "api/self/sites", "_id", site_scoped=False),
    ResourceSpec("unifi_radius_user", "rest/account", "_id"),
    # v2 with filters
    ResourceSpec("unifi_firewall_policy",
                 "v2/api/site/{site}/firewall-policies", "_id",
                 include={"predefined": False}),
    ResourceSpec("unifi_firewall_zone",
                 "v2/api/site/{site}/firewall/zone", "_id",
                 include={"default_zone": False}),
    ResourceSpec("unifi_traffic_route",
                 "v2/api/site/{site}/trafficroutes", "_id"),
    ResourceSpec("unifi_dns_record",
                 "v2/api/site/{site}/static-dns", "_id"),
    # remaining bare-_id collections
    ResourceSpec("unifi_wlan", "rest/wlanconf", "_id"),
    ResourceSpec("unifi_port_profile", "rest/portconf", "_id"),
    ResourceSpec("unifi_port_forward", "rest/portforward", "_id"),
    ResourceSpec("unifi_firewall_group", "rest/firewallgroup", "_id"),
    ResourceSpec("unifi_firewall_rule", "rest/firewallrule", "_id"),
    ResourceSpec("unifi_radius_profile", "rest/radiusprofile", "_id"),
    ResourceSpec("unifi_dynamic_dns", "rest/dynamicdns", "_id"),
    # rest/routing backs static routes; rest/usergroup backs client-QoS "ClientGroup"
    ResourceSpec("unifi_static_route", "rest/routing", "_id"),
    ResourceSpec("unifi_client_qos_rate", "rest/usergroup", "_id"),
    ResourceSpec("unifi_account", "rest/account", "_id"),  # alias — skipped by enumerator
)

# Endpoints probed by the coverage audit (coverage.py) beyond those MANIFEST
# maps. A populated, unmapped collection is a coverage gap; built-in defaults
# (attr_no_delete / attr_hidden_id) are accepted. An endpoint that later gains
# a MANIFEST spec is skipped automatically — mapped endpoints are derived from
# MANIFEST at runtime, never repeated here.
PROBE_ENDPOINTS: dict[str, str] = {
    "v2/api/site/{site}/nat": "NAT rules",
    "v2/api/site/{site}/content-filtering": "DNS content-filtering",
    "v2/api/site/{site}/apgroups": "AP groups",
    "v2/api/site/{site}/trafficrules": "traffic rules",
    "v2/api/site/{site}/qos-rules": "QoS rules",
    "v2/api/site/{site}/acl-rules": "switch ACL rules",
    "v2/api/site/{site}/wan-slas": "WAN SLA monitors",
    "v2/api/site/{site}/device-tags": "device tags",
    "rest/scheduletask": "scheduled tasks",
    "rest/dpigroup": "DPI groups",
    "rest/dpiapp": "DPI app rules",
    "rest/wlangroup": "WLAN groups (legacy)",
    "rest/hotspotop": "hotspot operators",
    "rest/hotspotpackage": "hotspot packages",
    "rest/hotspot2conf": "Hotspot 2.0 config",
    "rest/channelplan": "channel plans",
}

# Setting sections that are STRUCTURALLY out of scope — closable by neither a
# provider PR nor adoption. Deliberately tiny: everything else stays visible
# in COVERAGE.md until settled (acceptance = merging the PR that adds the
# line; silencing = a provider PR modeling the field, settable or
# computed+sensitive). Keys are fnmatch globs against the live section key.
CLASSIFIED_SECTIONS: dict[str, str] = {
    "super_*": "console-scope; a site-scoped unifi_setting cannot model it",
}


def specs_for_endpoint(endpoint: str) -> list[ResourceSpec]:
    return [s for s in MANIFEST if s.endpoint == endpoint]


def spec_for_type(resource_type: str) -> ResourceSpec:
    for s in MANIFEST:
        if s.resource_type == resource_type:
            return s
    raise KeyError(resource_type)
