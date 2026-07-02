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


def format_secret_suppressions(hits: list[str]) -> str:
    """Loud warning for secret-shaped values the safety net suppressed.

    Each hit is "<resource_type>.<slug>: <attr path>". The attr was omitted
    from the emitted HCL and added to lifecycle ignore_changes; managing it
    properly needs a SECRETS rule.
    """
    if not hits:
        return ""
    lines = "\n".join(f"  - {h}" for h in hits)
    return (
        "WARNING: secret-shaped value(s) suppressed — "
        "add a SECRETS rule to manage them:\n" + lines
    )


def is_secrets_only_diff(
    plan_json: dict[str, Any],
    sensitive_attrs_by_type: dict[str, set[str]],
) -> bool:
    for rc in plan_json.get("resource_changes", []):
        change = rc.get("change", {})
        actions = change.get("actions") or []

        # Skip no-op and read changes
        if actions in (["no-op"], ["read"]):
            continue

        # Structural changes (create, delete, replace) are never "secrets only"
        if "create" in actions or "delete" in actions:
            return False

        # For update-only changes, check if all diffs are in sensitive attrs
        before = change.get("before") or {}
        after = change.get("after") or {}
        allowed = sensitive_attrs_by_type.get(rc.get("type"), set())
        changed = {k for k in set(before) | set(after)
                   if before.get(k) != after.get(k)}
        if changed - allowed:
            return False

    return True
