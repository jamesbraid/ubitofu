# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 James Braid
"""What a provider bump breaks, read off the schema before anything plans.

Reconcile works from a plan, so it can only see changes tofu can express as a
diff. A provider that DROPS an attribute the config still sets breaks earlier
than that: `tofu plan` fails with "Unsupported argument" and there is no plan
JSON at all. Catching that needs the schema of the old version and the new one,
which is what this module diffs.

Known limitation, and the reason this is only half the gate: the schema JSON
carries no defaults. OpenTofu's serializer (internal/command/jsonprovider/
attribute.go) emits type, nested_type, description, description_kind,
deprecated, deprecation_message, required, optional, computed and write_only —
nothing else. A provider that starts defaulting an Optional+Computed attribute
looks identical here to one that does not, so an attribute merely showing up as
new is reported for review and the actual verdict comes from planning against
the live controller (reconcile's committed-config oracle handles that half).

New resource types are deliberately NOT reported: the coverage audit's
manifest-lag check already names every provider resource ubitofu does not map,
and reporting them here too would double up on every bump.
"""
import re
from typing import Any

from .coverage import Finding

# Flags per attribute — the subset of the schema JSON worth keeping in a
# baseline. Everything the diff below asks about is in here.
_FLAGS = ("required", "optional", "computed", "deprecated")

# Kinds that mean "this bump will not plan until you act".
BLOCKERS = frozenset({
    "removed-resource", "removed-attr", "new-required-attr", "optional-to-required",
})


def _attr_flags(spec: dict[str, Any]) -> dict[str, bool]:
    return {f: bool(spec.get(f)) for f in _FLAGS}


def _walk_block(block: dict[str, Any], prefix: str = "") -> dict[str, dict[str, bool]]:
    """Flatten one block's attributes to dotted paths.

    Recursion matters: the attribute that motivated this module lives at
    ``unifi_device.radio_table.assisted_roaming_enabled``, two levels down a
    nested_type, and a top-level-only walk would miss it entirely.
    """
    out: dict[str, dict[str, bool]] = {}
    for name, spec in block.get("attributes", {}).items():
        path = f"{prefix}{name}"
        out[path] = _attr_flags(spec)
        nested = spec.get("nested_type")
        if nested:
            out.update(_walk_block({"attributes": nested.get("attributes", {})},
                                   f"{path}."))
    for name, bt in block.get("block_types", {}).items():
        path = f"{prefix}{name}"
        # A nested config block: writable, never controller-assigned. Its own
        # required/computed flags do not exist in the JSON, so record the shape
        # the diff can act on and recurse for the leaves that matter.
        out[path] = {"required": False, "optional": True,
                     "computed": False, "deprecated": bool(bt.get("deprecated"))}
        out.update(_walk_block(bt.get("block", {}), f"{path}."))
    return out


def reduce_schema(schema: dict[str, Any]) -> dict[str, dict[str, dict[str, bool]]]:
    """Project ``tofu providers schema -json`` down to resource type -> path -> flags.

    Small enough to keep as a committed baseline, and the only part of the
    schema a version diff can act on.
    """
    out: dict[str, dict[str, dict[str, bool]]] = {}
    for prov in schema.get("provider_schemas", {}).values():
        for rtype, rschema in prov.get("resource_schemas", {}).items():
            out[rtype] = _walk_block(rschema.get("block", {}))
    return out


def diff_resources(
    baseline: dict[str, dict[str, dict[str, bool]]],
    current: dict[str, dict[str, dict[str, bool]]],
) -> list[Finding]:
    """Findings for everything the bump changed that a config can trip over."""
    findings: list[Finding] = []
    for rtype in sorted(set(baseline) | set(current)):
        old = baseline.get(rtype)
        new = current.get(rtype)
        if new is None:
            findings.append(Finding(
                "removed-resource", rtype,
                "gone from the provider — every block of this type fails to plan"))
            continue
        if old is None:
            continue  # new resource: the coverage audit's manifest-lag check owns it
        for path in sorted(set(old) | set(new)):
            o, n = old.get(path), new.get(path)
            ident = f"{rtype}.{path}"
            if n is None:
                findings.append(Finding(
                    "removed-attr", ident,
                    "removed — a config that sets it fails to plan"))
            elif o is None:
                if n["required"]:
                    findings.append(Finding(
                        "new-required-attr", ident,
                        "new and required — every block of this type must set it"))
                else:
                    findings.append(Finding(
                        "new-attr", ident,
                        "new — plan against live before applying: the schema JSON "
                        "cannot show whether it carries a default that would "
                        "override the controller's value"))
            else:
                if n["required"] and not o["required"]:
                    findings.append(Finding(
                        "optional-to-required", ident,
                        "now required — a block that leaves it out fails to plan"))
                if n["deprecated"] and not o["deprecated"]:
                    findings.append(Finding(
                        "deprecated-attr", ident,
                        "deprecated — plan to stop setting it"))
    return findings


def attr_locations(texts: dict[str, str], leaf: str) -> list[str]:
    """``file:line`` for every assignment of *leaf* in the committed HCL.

    Name-based, not path-precise: the surgeon only models top-level scalars,
    and the attributes that get removed are routinely nested. A same-named
    attribute on an unrelated resource would be reported too — an extra line
    naming a genuinely removed attribute, which is the safe direction to err.
    """
    pattern = re.compile(rf"^\s*{re.escape(leaf)}\s*=")
    return [f"{name}:{i}"
            for name, text in sorted(texts.items())
            for i, line in enumerate(text.splitlines(), 1)
            if pattern.match(line)]


def declares_type(texts: dict[str, str], rtype: str) -> bool:
    """Whether the committed HCL declares any block of *rtype*."""
    needle = re.compile(rf'resource\s+"{re.escape(rtype)}"\s')
    return any(needle.search(text) for text in texts.values())


def filter_to_config(findings: list[Finding], texts: dict[str, str]) -> list[Finding]:
    """Drop what this config cannot hit, and locate what it can.

    A finding about a resource type the config never declares is noise, and so
    is a removed attribute nothing sets. What survives is actionable: a removed
    attribute gets the file:line list appended, so the operator sees the edits
    the bump requires rather than a schema fact.
    """
    kept: list[Finding] = []
    for f in findings:
        rtype = f.identifier.split(".")[0]
        if not declares_type(texts, rtype):
            continue
        if f.kind == "removed-attr":
            leaf = f.identifier.rsplit(".")[-1]
            hits = attr_locations(texts, leaf)
            if not hits:
                continue
            f = Finding(f.kind, f.identifier, f"{f.detail}; set in {', '.join(hits)}")
        kept.append(f)
    return kept


_LOCK_PROVIDER_RE = re.compile(
    r'provider\s+"([^"]+)"\s*\{[^}]*?\bversion\s*=\s*"([^"]+)"', re.DOTALL)


def lock_versions(text: str) -> dict[str, str]:
    """Provider source -> installed version, read from ``.terraform.lock.hcl``.

    What tofu actually installed, not what the config asked for: the bump is
    detected from the same file that decides which binary runs.
    """
    return {m.group(1): m.group(2) for m in _LOCK_PROVIDER_RE.finditer(text)}
