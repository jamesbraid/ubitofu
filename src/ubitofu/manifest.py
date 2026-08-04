# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 James Braid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Literal

from .reconcile_model import (
    CollectionIdentityPolicy,
    ControllerFieldPolicy,
    GenerationNormalizationPolicy,
    LifecyclePolicy,
)
from .values import FrozenObject, FrozenValue, freeze_value


def _freeze_policy(value: FrozenObject | Mapping[str, object] | None) -> FrozenObject | None:
    if value is None or isinstance(value, FrozenObject):
        return value
    frozen: FrozenValue = freeze_value(value)
    if not isinstance(frozen, FrozenObject):
        raise ValueError("manifest object policy must be an object")
    return frozen


def _policy(value: Mapping[str, object]) -> FrozenObject:
    frozen = _freeze_policy(value)
    assert frozen is not None
    return frozen


@dataclass(frozen=True)
class ResourceSpec:
    resource_type: str
    endpoint: str
    id_rule: Literal["_id", "site:_id", "mac", "mac_or_id", "site", "wg_two_level"]
    site_scoped: bool = True
    discriminator: FrozenObject | None = None
    include: FrozenObject | None = None
    # Singleton (id_rule="site") to skip when its config endpoint is empty:
    # the by-site import fails when no remote object exists (e.g. BGP unset).
    skip_if_empty: bool = False
    lifecycle: LifecyclePolicy = LifecyclePolicy(False, "attention")
    generation_normalization: GenerationNormalizationPolicy = (
        GenerationNormalizationPolicy("identity")
    )
    collection_identities: tuple[CollectionIdentityPolicy, ...] = ()
    controller_fields: tuple[ControllerFieldPolicy, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "discriminator", _freeze_policy(self.discriminator))
        object.__setattr__(self, "include", _freeze_policy(self.include))
        _validate_spec(self)

def _validate_spec(spec: ResourceSpec) -> None:
    if not spec.resource_type or not spec.endpoint:
        raise ValueError("manifest resource type and endpoint must be non-empty")
    if spec.id_rule not in {"_id", "site:_id", "mac", "mac_or_id", "site", "wg_two_level"}:
        raise ValueError("manifest identity rule is invalid")
    if (
        not isinstance(spec.lifecycle, LifecyclePolicy)
        or spec.lifecycle.deletion_policy not in {"capture", "attention", "forbid"}
    ):
        raise ValueError("manifest lifecycle policy is invalid")
    if (
        not isinstance(spec.generation_normalization, GenerationNormalizationPolicy)
        or spec.generation_normalization.rule
        not in {"identity", "port_forward_wan_all_to_both"}
    ):
        raise ValueError("manifest generation normalization policy is invalid")
    collection_paths: set[tuple[str | int, ...]] = set()
    for collection_policy in spec.collection_identities:
        if (
            not isinstance(collection_policy, CollectionIdentityPolicy)
            or not collection_policy.attribute_path
            or not collection_policy.identity_attribute
            or collection_policy.attribute_path in collection_paths
        ):
            raise ValueError("manifest collection identity policy is invalid")
        collection_paths.add(collection_policy.attribute_path)
    provider_paths: set[tuple[str | int, ...]] = set()
    for controller_policy in spec.controller_fields:
        if (
            not isinstance(controller_policy, ControllerFieldPolicy)
            or not controller_policy.controller_path
            or not controller_policy.provider_path
            or controller_policy.provider_path in provider_paths
            or controller_policy.coercion not in {"identity", "bool", "int", "string", "set"}
        ):
            raise ValueError("manifest controller field policy is invalid")
        provider_paths.add(controller_policy.provider_path)


def validate_manifest(specs: Iterable[ResourceSpec]) -> tuple[ResourceSpec, ...]:
    """Validate one complete manifest before any caller can consume it."""
    validated = tuple(specs)
    resource_types: set[str] = set()
    for spec in validated:
        _validate_spec(spec)
        if spec.resource_type in resource_types:
            raise ValueError("duplicate manifest resource type")
        resource_types.add(spec.resource_type)
    return validated


MANIFEST: tuple[ResourceSpec, ...] = (
    # rest/networkconf — one endpoint, five resources, discriminated on purpose
    ResourceSpec(
        "unifi_network",
        "rest/networkconf",
        "_id",
        discriminator=_policy({"purpose": "corporate|vlan-only"}),
        controller_fields=(ControllerFieldPolicy(("enabled",), ("enabled",), "bool"),),
    ),
    ResourceSpec("unifi_wan", "rest/networkconf", "_id",
                 discriminator=_policy({"purpose": "wan"})),
    ResourceSpec("unifi_vpn_server", "rest/networkconf", "_id",
                 discriminator=_policy({"purpose": "remote-user-vpn"})),
    ResourceSpec("unifi_vpn_client", "rest/networkconf", "_id",
                 discriminator=_policy({"purpose": "vpn-client"})),
    ResourceSpec("unifi_site_to_site_vpn", "rest/networkconf", "_id",
                 discriminator=_policy({"purpose": "site-vpn", "vpn_type": "ipsec"})),
    # MAC-keyed
    ResourceSpec("unifi_client", "rest/user", "mac",
                 include=_policy({"fixed_ip": "__present__"})),
    ResourceSpec(
        "unifi_device",
        "stat/device",
        "mac_or_id",
        lifecycle=LifecyclePolicy(True, "capture"),
        collection_identities=(CollectionIdentityPolicy(("port_override",), "port_idx"),),
    ),
    # Keyed by `id`, NOT `mac`: the v2 record carries the supervised device's MAC
    # as `client_mac` and has no `mac`/`_id` key (id_rule="mac" derived None here
    # and aborted every reconcile). Enumerated but never adopted — see the
    # power_supervisor skip in enumerator._skip_reason.
    ResourceSpec("unifi_power_supervisor",
                 "v2/api/site/{site}/power-supervisors", "_id"),
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
                 include=_policy({"predefined": False})),
    ResourceSpec("unifi_firewall_zone",
                 "v2/api/site/{site}/firewall/zone", "_id",
                 include=_policy({"default_zone": False})),
    ResourceSpec("unifi_traffic_route",
                 "v2/api/site/{site}/trafficroutes", "_id"),
    ResourceSpec("unifi_dns_record",
                 "v2/api/site/{site}/static-dns", "_id"),
    # remaining bare-_id collections
    ResourceSpec("unifi_wlan", "rest/wlanconf", "_id"),
    ResourceSpec("unifi_port_profile", "rest/portconf", "_id"),
    ResourceSpec(
        "unifi_port_forward",
        "rest/portforward",
        "_id",
        generation_normalization=GenerationNormalizationPolicy(
            "port_forward_wan_all_to_both"
        ),
    ),
    ResourceSpec("unifi_firewall_group", "rest/firewallgroup", "_id"),
    ResourceSpec("unifi_firewall_rule", "rest/firewallrule", "_id"),
    ResourceSpec("unifi_radius_profile", "rest/radiusprofile", "_id"),
    ResourceSpec("unifi_dynamic_dns", "rest/dynamicdns", "_id"),
    # rest/routing backs static routes; rest/usergroup backs client-QoS "ClientGroup"
    ResourceSpec("unifi_static_route", "rest/routing", "_id"),
    ResourceSpec("unifi_client_qos_rate", "rest/usergroup", "_id"),
    ResourceSpec("unifi_account", "rest/account", "_id"),  # alias — skipped by enumerator
)

MANIFEST = validate_manifest(MANIFEST)

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
