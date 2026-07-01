from typing import Any


def format_gaps(gaps: list[str]) -> str:
    if not gaps:
        return "Coverage: no coverage gaps detected."
    return "Coverage gaps:\n" + "\n".join(f"  - {g}" for g in gaps)


def format_drift(plan_json: dict[str, Any]) -> str:
    lines = []
    for rc in plan_json.get("resource_changes", []):
        actions = rc.get("change", {}).get("actions", [])
        if actions == ["no-op"] or actions == ["read"]:
            continue
        lines.append(f"  {'/'.join(actions):8} {rc['address']}")
    if not lines:
        return "Drift: 0 changes (clean plan)."
    return "Drift:\n" + "\n".join(lines)


def is_secrets_only_diff(
    plan_json: dict[str, Any],
    sensitive_attrs_by_type: dict[str, set[str]],
) -> bool:
    for rc in plan_json.get("resource_changes", []):
        change = rc.get("change", {})
        if change.get("actions") in (["no-op"], ["read"], ["create"], None):
            if change.get("actions") == ["create"]:
                return False
            continue
        before = change.get("before") or {}
        after = change.get("after") or {}
        allowed = sensitive_attrs_by_type.get(rc.get("type"), set())
        changed = {k for k in set(before) | set(after)
                   if before.get(k) != after.get(k)}
        if changed - allowed:
            return False
    return True
