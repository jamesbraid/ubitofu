import re
from dataclasses import dataclass


@dataclass(frozen=True)
class VarRef:
    expr: str


# Value-pattern secret safety net (the WireGuard lesson): the provider can
# return secret material in PLAINTEXT with no schema `sensitive` flag (live
# WG server private keys did exactly that). Detect secret-shaped content in
# already-cleaned attrs so it never reaches emitted HCL.
_SECRET_NAME_RE = re.compile(r"private_key|passphrase|secret|token|password", re.IGNORECASE)
_PUBLIC_NAME_RE = re.compile(r"public", re.IGNORECASE)
# curve25519/WireGuard key shape: 44 chars of base64 ending in '='.
_B64_KEY_RE = re.compile(r"^[A-Za-z0-9+/]{43}=$")


def _is_secret_shaped(name: str, value: object) -> bool:
    if not isinstance(value, str) or not value:
        return False
    if _PUBLIC_NAME_RE.search(name):
        # WG PUBLIC keys share the b64 shape but are not secrets (and may be
        # required attrs, e.g. unifi_wireguard_peer.public_key).
        return False
    return bool(_SECRET_NAME_RE.search(name) or _B64_KEY_RE.match(value))


def strip_secret_shaped(attrs: dict, _prefix: str = "") -> list[str]:  # type: ignore[type-arg]
    """Remove secret-shaped string values from cleaned attrs (mutating).

    A hit is (a) a string value whose attr NAME looks secret-bearing
    (private_key/passphrase/secret/token/password, case-insensitive), or
    (b) any string value shaped like a curve25519/WireGuard key. VarRef
    values are already var-sourced and never match; attr names containing
    "public" are exempt. Containers emptied by stripping are dropped too.

    Returns the dotted paths of removed values, for reporting and for
    adding their top-level attrs to lifecycle ignore_changes.
    """
    hits: list[str] = []
    for name in list(attrs):
        value = attrs[name]
        path = f"{_prefix}{name}"
        if isinstance(value, VarRef):
            continue
        if isinstance(value, dict):
            hits.extend(strip_secret_shaped(value, f"{path}."))
            if not value:
                del attrs[name]
        elif isinstance(value, list):
            if any(isinstance(e, str) and _is_secret_shaped(name, e) for e in value):
                hits.append(path)
                del attrs[name]
                continue
            for i, entry in enumerate(value):
                if isinstance(entry, dict):
                    hits.extend(strip_secret_shaped(entry, f"{path}[{i}]."))
            remaining = [e for e in value if not (isinstance(e, dict) and not e)]
            if remaining:
                attrs[name] = remaining
            else:
                del attrs[name]
        elif _is_secret_shaped(name, value):
            hits.append(path)
            del attrs[name]
    return hits


def is_settable(attr_schema: dict) -> bool:  # type: ignore[type-arg]
    required = bool(attr_schema.get("required"))
    optional = bool(attr_schema.get("optional"))
    return required or optional


def is_empty(value: object) -> bool:
    return value is None or value == "" or value == [] or value == {}


def normalize_emitted(resource_type: str, attrs: dict) -> dict:  # type: ignore[type-arg]
    """Fix up specific attribute values the provider rejects verbatim.

    Small, explicit per-resource/attr normalizations applied to already-cleaned
    attrs before rendering — NOT a blanket string replace.

    - unifi_port_forward.wan.interface: the controller reports "all" for a
      forward that applies to every WAN, but the provider validator accepts
      only wan/wan2/both. With 2 WANs, "both" is the equivalent.
    """
    if resource_type == "unifi_port_forward":
        wan = attrs.get("wan")
        if isinstance(wan, dict) and wan.get("interface") == "all":
            wan["interface"] = "both"
    return attrs


def clean_resource(
    values: dict,  # type: ignore[type-arg]
    resource_schema: dict,  # type: ignore[type-arg]
    sensitive: dict[str, VarRef] | None = None,
) -> dict:  # type: ignore[type-arg]
    sensitive = sensitive or {}
    block = resource_schema["block"]
    attrs = block.get("attributes", {})
    out: dict[str, object] = {}

    for name, schema in attrs.items():
        if name in sensitive:
            out[name] = sensitive[name]
            continue
        if not is_settable(schema):
            continue
        value = values.get(name)
        if is_empty(value):
            continue
        # Single/collection nested-object ATTRIBUTES live under `nested_type`
        # (not block_types). generate-config-out emits every child verbatim,
        # including null/empty and computed-only fields (e.g. firewall_policy
        # source/destination) — recurse so the settable/drop-empty rules apply
        # inside them too, else read-only children leak and break the plan.
        nested = schema.get("nested_type")
        if nested:
            assert value is not None  # is_empty guards None; narrows for mypy
            sub_schema = {"block": {"attributes": nested["attributes"]}}
            if nested.get("nesting_mode") == "single":
                single = clean_resource(value, sub_schema)
                if single:
                    out[name] = single
            else:  # list/set nesting -> a list of object entries
                nested_entries: list[dict[str, object]] = (
                    value if isinstance(value, list) else [value])
                nested_cleaned = [c for e in nested_entries
                                  if (c := clean_resource(e, sub_schema))]
                if nested_cleaned:
                    out[name] = nested_cleaned
            continue
        out[name] = value

    # Repeated/nested config blocks live under block_types (not attributes).
    # Previously these were silently dropped, making 0-change plans unreachable.
    for name, bt in block.get("block_types", {}).items():
        raw = values.get(name)
        if is_empty(raw):
            continue
        assert raw is not None  # is_empty guards None; assert narrows for mypy
        entries: list[dict[str, object]] = raw if isinstance(raw, list) else [raw]
        cleaned = [clean_resource(e, {"block": bt["block"]}) for e in entries]
        cleaned = [c for c in cleaned if c]  # drop wholly-empty entries
        if cleaned:
            out[name] = cleaned

    return out
