from dataclasses import dataclass


@dataclass(frozen=True)
class VarRef:
    expr: str


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
