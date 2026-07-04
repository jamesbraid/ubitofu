# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 James Braid
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


def format_secret_sources(op_refs: dict[str, str]) -> str:
    """Tell the operator where each secret variable's value should come from.

    References are printed, never written to files.
    """
    if not op_refs:
        return ""
    lines = "\n".join(f"  var.{name}  <-  {ref}"
                      for name, ref in sorted(op_refs.items()))
    return "Secret variable sources (supply values from your secret manager):\n" + lines


def format_reconcile(
    merged: list[str],
    complex_flags: list[str],
    appended: list[str],
    removed: list[str],
    *,
    secret_warnings: list[str] | None = None,
) -> str:
    """Render the reconcile report — the product of a reconcile run.

    Four sections: values auto-merged from live into committed HCL, drift too
    complex to auto-edit (flagged for manual review), new controller objects
    appended, and resources present in committed config but diverged/gone on the
    controller (flagged, never auto-deleted).  An optional fifth section lists
    secret variables introduced by newly-appended objects so the operator knows
    to declare them and set TF_VAR_<name>.
    """
    sections: list[str] = []

    def _sec(title: str, items: list[str]) -> None:
        if items:
            sections.append(title + "\n" + "\n".join(f"  - {i}" for i in items))

    _sec("Auto-merged (committed <- live):", merged)
    _sec("Flagged for manual review (complex drift):", complex_flags)
    _sec("Appended (new controller objects):", appended)
    _sec("Flagged removed (in config, diverged on controller):", removed)
    if secret_warnings:
        items = [
            f"new object uses secret var {name} — declare it + set TF_VAR_{name}"
            for name in secret_warnings
        ]
        sections.append("Secret variable warnings:\n"
                        + "\n".join(f"  - {i}" for i in items))
    if not sections:
        return "Reconcile: already in sync — no changes."
    return "Reconcile report:\n" + "\n\n".join(sections)


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
