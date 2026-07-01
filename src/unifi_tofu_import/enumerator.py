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


def extract_id(obj: dict[str, object], spec: ResourceSpec, site: str) -> str:
    if spec.id_rule == "site":
        return site
    if spec.id_rule == "mac":
        return str(obj["mac"])
    if spec.id_rule == "mac_or_id":
        return str(obj.get("mac") or obj["_id"])
    if spec.id_rule == "site:_id":
        return f"{site}:{obj['_id']}"
    return str(obj["_id"])


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
            result.targets.append(
                ImportTarget(spec.resource_type, _name_hint({}, spec, ctl.site), ctl.site))
            continue
        if spec.id_rule == "wg_two_level":
            result.targets.extend(_enumerate_wireguard(ctl, spec))
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
    """
    targets: list[ImportTarget] = []
    nets = [n for n in ctl.collection("rest/networkconf")
            if n.get("vpn_type") == "wireguard-server"]
    for net in nets:
        nid = str(net["_id"])
        for peer in ctl.collection(f"v2/api/site/{{site}}/wireguard/{nid}/users"):
            targets.append(ImportTarget(
                "unifi_wireguard_peer",
                str(peer.get("name") or peer["_id"]),
                f"{nid}:{peer['_id']}"))
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
