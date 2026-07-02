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

# Collections with no provider resource. Enumerator flags populated ones loudly.
UNMAPPED_ENDPOINTS: dict[str, str] = {
    "v2/api/site/{site}/nat": "NAT rules",
    "v2/api/site/{site}/content-filtering": "DNS content-filtering",
    "v2/api/site/{site}/apgroups": "AP groups (no provider resource)",
}


def specs_for_endpoint(endpoint: str) -> list[ResourceSpec]:
    return [s for s in MANIFEST if s.endpoint == endpoint]


def spec_for_type(resource_type: str) -> ResourceSpec:
    for s in MANIFEST:
        if s.resource_type == resource_type:
            return s
    raise KeyError(resource_type)
