# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 James Braid
from typing import Any


def format_gaps(gaps: list[str]) -> str:
    if not gaps:
        return "Coverage: no coverage gaps detected."
    return "Coverage gaps:\n" + "\n".join(f"  - {g}" for g in gaps)


def format_coverage(gap_lines: list[str], accepted_count: int) -> str:
    """One home for all coverage output: enumeration gaps + audit findings.

    ``gap_lines`` is EnumerationResult.gaps + CoverageReport.gap_lines();
    accepted items are counted with a pointer to COVERAGE.md, never hidden.
    """
    out = format_gaps(gap_lines)
    if accepted_count:
        out += (f"\n  ({accepted_count} accepted item(s) "
                "— see COVERAGE.md for reasons)")
    return out


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


_DIVERGED_LABELS: dict[str, str] = {
    "deleted": "deleted on controller — remove from config or re-adopt",
    "pending": "in config, not yet applied — run apply",
    "diverged": "in committed config, controller state diverged",
}


def format_reconcile(
    merged: list[str],
    complex_flags: list[str],
    appended: list[str],
    *,
    secret_warnings: list[str] | None = None,
    orphaned: list[str] | None = None,
    diverged: list[tuple[str, str]] | None = None,
    removed: list[str] | None = None,
    codified: list[str] | None = None,
    forbidden: list[str] | None = None,
) -> str:
    """Render the reconcile report — the product of a reconcile run.

    Three sections: values auto-merged from live into committed HCL, drift too
    complex to auto-edit (flagged for manual review), and new controller objects
    appended.  An optional fourth section lists secret variables introduced by
    newly-appended objects so the operator knows to declare them and set
    TF_VAR_<name>.  An optional fifth section flags resources present in state
    but absent from committed config that tofu would DESTROY on apply.  An
    optional sixth section classifies committed-config resources whose plan
    diverged: deleted on controller, not yet applied, or generically diverged.
    An optional seventh section lists committed blocks deleted in the working
    tree because the controller object is gone.  An optional eighth section
    lists live state-only orphans appended to config instead of destroyed.  An
    optional ninth section — rendered first, since it is the most severe
    finding and drives exit 13 — names planned creates of UI-only lifecycle
    resources (currently unifi_device) that no apply may execute.
    """
    sections: list[str] = []

    def _sec(title: str, items: list[str]) -> None:
        if items:
            sections.append(title + "\n" + "\n".join(f"  - {i}" for i in items))

    if forbidden:
        items = [f"{addr} — tofu can never create a device; remove the block "
                 "or adopt in the UI and reconcile" for addr in forbidden]
        sections.append("Forbidden (device create — adoption is UI-only):\n"
                        + "\n".join(f"  - {i}" for i in items))
    _sec("Auto-merged (committed <- live):", merged)
    _sec("Flagged for manual review (complex drift):", complex_flags)
    _sec("Appended (new controller objects):", appended)
    _sec("Removed (deleted on controller):", removed or [])
    _sec("Codified (state-only → config):", codified or [])
    if secret_warnings:
        items = [
            f"new object uses secret var {name} — declare it + set TF_VAR_{name}"
            for name in secret_warnings
        ]
        sections.append("Secret variable warnings:\n"
                        + "\n".join(f"  - {i}" for i in items))
    if orphaned:
        items = [f"⚠ {addr} — would be DESTROYED on apply" for addr in orphaned]
        sections.append("Orphaned state (in state, not in committed config — would be DESTROYED):\n"
                        + "\n".join(f"  - {i}" for i in items))
    if diverged:
        items = [
            f"⚠ {addr} — {_DIVERGED_LABELS.get(tag, tag)}"
            for addr, tag in diverged
        ]
        sections.append("Flagged diverged (in config, plan diverged):\n"
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
