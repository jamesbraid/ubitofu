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
    return str(obj.get("name") or obj.get("hostname") or obj.get("_id") or site)


def enumerate_controller(
    ctl: Controller, manifest: Iterable[ResourceSpec] = MANIFEST
) -> EnumerationResult:
    result = EnumerationResult()
    specs = list(manifest)
    for spec in specs:
        if spec.resource_type in _ALIAS_SKIP:
            continue
        if spec.id_rule == "site":  # singleton — import by site, not enumerated
            result.targets.append(
                ImportTarget(spec.resource_type, _name_hint({}, spec, ctl.site), ctl.site))
            continue
        if spec.id_rule == "wg_two_level":
            result.targets.extend(_enumerate_wireguard(ctl, spec))
            continue
        for obj in ctl.collection(spec.endpoint):
            if matches(obj, spec):
                result.targets.append(ImportTarget(
                    spec.resource_type,
                    _name_hint(obj, spec, ctl.site),
                    extract_id(obj, spec, ctl.site)))
    result.gaps.extend(_coverage_gaps(ctl))
    return result


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
