# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 James Braid
from collections.abc import Iterable
from dataclasses import dataclass, field

from .controller import Controller
from .manifest import MANIFEST, UNMAPPED_ENDPOINTS, ResourceSpec


@dataclass(frozen=True)
class ImportTarget:
    resource_type: str
    name_hint: str
    import_id: str


@dataclass
class EnumerationResult:
    targets: list[ImportTarget] = field(default_factory=list)
    gaps: list[str] = field(default_factory=list)


_ALIAS_SKIP = {"unifi_account"}  # rest/account alias — unifi_radius_user wins

# Per-object skips: objects the provider cannot represent. Each maps to a
# coverage-gap label (rendered with the skipped count) instead of being emitted
# as invalid config. Keyed by the reason so counts aggregate across a run.
_SKIP_LABELS: dict[str, str] = {
    "app_policy": "app-based firewall policy(ies) — unsupported matching_target=APP",
    "radius_default": "default radius profile(s) — required auth_server.secret is "
                      "not sourceable; skipped",
    "usergroup_default": "default client-QoS usergroup(s) — unmanageable default "
                         "(-1 sentinel rates); skipped",
}


def _is_app_policy(obj: dict[str, object]) -> bool:
    """A firewall policy is app-based when source/destination match on APP.

    The provider's matching_target validator accepts ANY/NETWORK/CLIENT/IP/
    DEVICE/MAC/WEB but not APP (DPI / app_ids), so such policies cannot be
    represented and must be skipped.
    """
    for side in ("source", "destination"):
        sub = obj.get(side)
        if isinstance(sub, dict) and str(sub.get("matching_target")).upper() == "APP":
            return True
    return False


def _skip_reason(spec: ResourceSpec, obj: dict[str, object]) -> str | None:
    rt = spec.resource_type
    if rt == "unifi_firewall_policy" and _is_app_policy(obj):
        return "app_policy"
    # attr_no_delete marks a built-in/default object (like firewall `predefined`).
    # The lone default radius profile / QoS usergroup carry unsourceable secrets
    # or -1 sentinel rates the provider rejects — skip rather than emit invalid.
    if rt == "unifi_radius_profile" and obj.get("attr_no_delete"):
        return "radius_default"
    if rt == "unifi_client_qos_rate" and obj.get("attr_no_delete"):
        return "usergroup_default"
    return None


def matches(obj: dict[str, object], spec: ResourceSpec) -> bool:
    if spec.discriminator:
        for key, allowed in spec.discriminator.items():
            if str(obj.get(key)) not in allowed.split("|"):
                return False
    if spec.include:
        for key, want in spec.include.items():
            if want == "__present__":
                if not obj.get(key):
                    return False
            elif obj.get(key) != want:
                return False
    return True


def derive_identity(id_rule: str, record: dict[str, object], site: str) -> str | None:
    """Single source of truth for resource identity derivation.

    Tolerates both controller objects (object id in ``_id``) and tofu state
    rows (object id in ``id``), using ``record.get("_id") or record.get("id")``
    for rules that need the object id.

    An unknown ``id_rule`` raises ``ValueError`` immediately so a developer
    adding a new rule without updating this function gets a loud failure rather
    than a silent wrong identity.

    id_rules handled:
    - ``_id``         — bare object id (most resources)
    - ``mac``         — MAC address (unifi_client, unifi_power_supervisor)
    - ``mac_or_id``   — MAC if present, otherwise object id (unifi_device)
    - ``site``        — site name singleton; identity is the site itself
    - ``site:_id``    — composite "<site>:<object_id>" (unused in manifest;
                        kept correct so it can't bite when first used)
    - ``wg_two_level``— composite "<network_id>:<peer_id>" for wireguard_peer;
                        controller records must be augmented with network_id
                        by the caller (_enumerate_wireguard injects it)
    """
    if id_rule == "_id":
        oid = record.get("_id") or record.get("id")
        return str(oid) if oid is not None else None
    if id_rule == "mac":
        mac = record.get("mac")
        return str(mac) if mac is not None else None
    if id_rule == "mac_or_id":
        mac = record.get("mac")
        if mac:
            return str(mac)
        oid = record.get("_id") or record.get("id")
        return str(oid) if oid is not None else None
    if id_rule == "site":
        # Singletons: the enumerator emits the site name as the import_id;
        # the provider stores id = site_name in state. Both sides call this
        # function with the actual site string — no record field needed.
        return site
    if id_rule == "site:_id":
        oid = record.get("_id") or record.get("id")
        return f"{site}:{oid}" if oid is not None else None
    if id_rule == "wg_two_level":
        # Enumerator augments each peer record with network_id before calling
        # here; state rows carry network_id natively.
        nid = record.get("network_id")
        pid = record.get("_id") or record.get("id")
        return f"{nid}:{pid}" if nid is not None and pid is not None else None
    raise ValueError(f"unknown id_rule: {id_rule!r}")


def extract_id(obj: dict[str, object], spec: ResourceSpec, site: str) -> str:
    """Derive the import_id for a live controller object.

    Delegates to derive_identity so both the controller side and the tofu-state
    side use identical logic — drift between the two is structurally impossible.
    """
    result = derive_identity(spec.id_rule, obj, site)
    if result is None:
        raise ValueError(
            f"cannot derive identity for {spec.resource_type} "
            f"(id_rule={spec.id_rule!r}) from {obj!r}"
        )
    return result


def _name_hint(obj: dict[str, object], spec: ResourceSpec, site: str) -> str:
    if spec.id_rule == "site":
        return spec.resource_type.removeprefix("unifi_")
    # `key`/`host_name` cover records that leave `name` null: unifi_dns_record
    # (static-dns keys the hostname under `key`) and unifi_dynamic_dns
    # (host_name) — so `to =` labels read as the hostname, not n_<hex>.
    return str(obj.get("name") or obj.get("hostname") or obj.get("host_name")
               or obj.get("key") or obj.get("_id") or site)


def enumerate_controller(
    ctl: Controller, manifest: Iterable[ResourceSpec] = MANIFEST
) -> EnumerationResult:
    result = EnumerationResult()
    specs = list(manifest)
    skipped: dict[str, int] = {}
    for spec in specs:
        if spec.resource_type in _ALIAS_SKIP:
            continue
        if spec.id_rule == "site":  # singleton — import by site, not enumerated
            if spec.skip_if_empty and not ctl.collection(spec.endpoint):
                result.gaps.append(
                    f"{spec.resource_type} skipped — not configured "
                    "(no remote object to import)")
                continue
            hint = _name_hint({}, spec, ctl.site)  # pragma: no mutate — equivalent: id_rule=="site" branch of _name_hint ignores its obj ({}) and site args (returns resource_type.removeprefix); the spec arg is exercised by test_singleton_setting_imports_by_site  # noqa: E501
            result.targets.append(ImportTarget(spec.resource_type, hint, ctl.site))
            continue
        if spec.id_rule == "wg_two_level":
            result.targets.extend(_enumerate_wireguard(ctl, spec))  # pragma: no mutate — equivalent: _enumerate_wireguard never references its spec param; the ctl arg is exercised by test_wireguard_two_level  # noqa: E501
            continue
        for obj in ctl.collection(spec.endpoint):
            if not matches(obj, spec):
                continue
            reason = _skip_reason(spec, obj)
            if reason is not None:
                skipped[reason] = skipped.get(reason, 0) + 1
                continue
            result.targets.append(ImportTarget(
                spec.resource_type,
                _name_hint(obj, spec, ctl.site),
                extract_id(obj, spec, ctl.site)))
    for reason, count in skipped.items():
        result.gaps.append(f"{count} {_SKIP_LABELS[reason]}")
    result.gaps.extend(_guest_network_gaps(ctl, specs))
    result.gaps.extend(_coverage_gaps(ctl))
    return result


def _guest_network_gaps(
    ctl: Controller, specs: list[ResourceSpec]
) -> list[str]:
    """Report guest networks (purpose="guest") — no provider resource exists.

    They are already excluded by the unifi_network discriminator; this counts
    and reports them so the coverage gap is explicit rather than silent.
    """
    if not any(s.endpoint == "rest/networkconf" for s in specs):
        return []
    n = sum(1 for net in ctl.collection("rest/networkconf")
            if net.get("purpose") == "guest")
    if not n:
        return []
    return [f"{n} guest network(s) — no provider resource; not imported"]


def _enumerate_wireguard(ctl: Controller, spec: ResourceSpec) -> list[ImportTarget]:
    """First list WG-server networks, then GET each server's users.

    Import id is "network_id:peer_id" (two-level enumeration).
    Injects network_id into each peer record so derive_identity can construct
    the composite identity — keeping enumerator and state-side logic in sync.
    """
    targets: list[ImportTarget] = []
    nets = [n for n in ctl.collection("rest/networkconf")
            if n.get("vpn_type") == "wireguard-server"]
    for net in nets:
        nid = str(net["_id"])
        for peer in ctl.collection(f"v2/api/site/{{site}}/wireguard/{nid}/users"):
            # Augment the peer with network_id so derive_identity can build the
            # composite "nid:peer_id" without needing a separate parameter.
            record = {**peer, "network_id": nid}
            import_id = derive_identity("wg_two_level", record, ctl.site)  # pragma: no mutate — equivalent: derive_identity's wg_two_level branch builds "{network_id}:{_id}" and never reads site; rule literal + record are exercised by test_wireguard_two_level  # noqa: E501
            if import_id is None:
                continue  # malformed peer (no _id) — skip rather than crash
            targets.append(ImportTarget(
                "unifi_wireguard_peer",
                str(peer.get("name") or peer.get("_id") or import_id),
                import_id))
    return targets


def _coverage_gaps(ctl: Controller) -> list[str]:
    """Flag any UNMAPPED_ENDPOINTS collection that is non-empty."""
    gaps: list[str] = []
    for endpoint, label in UNMAPPED_ENDPOINTS.items():
        n = len(ctl.collection(endpoint))
        if n:
            gaps.append(
                f"{n} objects found at {endpoint} ({label})"
                " with no provider resource — not imported")
    return gaps
