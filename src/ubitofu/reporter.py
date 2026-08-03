# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 James Braid
from typing import Any

from .schema_diff import BLOCKERS


def format_gaps(gaps: list[str]) -> str:
    if not gaps:
        return "Coverage: no coverage gaps detected."
    return "Coverage gaps:\n" + "\n".join(f"  - {g}" for g in gaps)


def format_migrate(
    findings: list[Any],
    baseline_versions: dict[str, str],
    current_versions: dict[str, str],
) -> str:
    """Render the provider-migration report — the product of `migrate`.

    Blocking findings come first: they stop a plan from running at all. Review
    findings follow. The schema JSON cannot settle those either way, so only a
    plan against the live controller can.
    """
    moved = sorted(
        f"{src} {baseline_versions.get(src, '(absent)')} -> {ver}"
        for src, ver in current_versions.items()
        if baseline_versions.get(src) != ver
    )
    head = ("Provider migration: " + "; ".join(moved) if moved
            else "Provider migration: no provider version moved since the baseline")
    if not findings:
        return head + "\n  no config-visible schema changes."
    blocking = [f for f in findings if f.kind in BLOCKERS]
    review = [f for f in findings if f.kind not in BLOCKERS]
    out = head
    if blocking:
        out += ("\nBlocking — the plan fails until these are resolved:\n"
                + "\n".join(f"  - {f.line()}" for f in blocking))
    if review:
        out += ("\nReview — plan against live before applying:\n"
                + "\n".join(f"  - {f.line()}" for f in review))
    return out


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


def format_reconcile(
    merged: list[str],
    complex_flags: list[str],
    appended: list[str],
    *,
    secret_warnings: list[str] | None = None,
    removed: list[str] | None = None,
    forbidden: list[str] | None = None,
    imported: list[str] | None = None,
    pending: list[tuple[str, str]] | None = None,
    existence_attention: list[str] | None = None,
) -> str:
    """Render every reconcile decision in severity and mutation order."""
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
    _sec("Imported into existing config:", imported or [])
    _sec("Removed (deleted on controller):", removed or [])
    _sec("Requires attention (resource existence):", existence_attention or [])
    if pending:
        _sec(
            "Pending apply (config intent not yet applied):",
            [f"{address} — {direction}" for address, direction in pending],
        )
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
