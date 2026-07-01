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
