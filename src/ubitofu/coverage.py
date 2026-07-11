# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 James Braid
"""Schema-driven provider-coverage audit.

Every live controller item lands in exactly one bucket: managed (MANIFEST +
provider schema), gap (live config the schema cannot express), or accepted
(structurally out of scope, with a written reason). There is no per-item
ignore list: acceptance happens in git by merging the COVERAGE.md change,
and gaps are silenced at the source of truth by provider PRs (settable
attributes for real config, computed + sensitive for controller internals).
"""
from dataclasses import dataclass, field
from typing import Any


def _norm(name: str) -> str:
    """Normalize a field name so API camelCase matches schema snake_case."""
    return name.replace("_", "").lower()


@dataclass(frozen=True)
class Finding:
    kind: str        # "section" | "field" | "endpoint" | "resource" | "object"
    identifier: str  # section key, "section.field", endpoint, resource type
    detail: str

    def line(self) -> str:
        return f"{self.kind} {self.identifier}: {self.detail}"


def _sorted_lines(findings: list[Finding]) -> list[str]:
    return [f.line() for f in
            sorted(findings, key=lambda f: (f.kind, f.identifier, f.detail))]


@dataclass
class CoverageReport:
    gaps: list[Finding] = field(default_factory=list)
    accepted: list[Finding] = field(default_factory=list)

    def gap_lines(self) -> list[str]:
        return _sorted_lines(self.gaps)


# unifi_setting attributes that are not controller sections.
_NON_SECTION_ATTRS = frozenset({"site", "id", "timeouts"})


def setting_schema_sections(schema: dict[str, Any]) -> dict[str, set[str]]:
    """Map unifi_setting attribute name -> normalized nested-field names.

    Reads the resource out of `tofu providers schema -json`. Raises KeyError
    when no provider in the schema defines unifi_setting — the audit must
    never run blind (a missing schema would reintroduce silent ignoring).
    """
    for prov in schema["provider_schemas"].values():
        rs = prov.get("resource_schemas", {})
        if "unifi_setting" not in rs:
            continue
        attrs = rs["unifi_setting"]["block"]["attributes"]
        out: dict[str, set[str]] = {}
        for name, spec in attrs.items():
            if name in _NON_SECTION_ATTRS:
                continue
            nested = spec.get("nested_type", {}).get("attributes", {})
            out[name] = {_norm(f) for f in nested}
        return out
    raise KeyError("unifi_setting not found in provider schema")


def schema_resource_types(schema: dict[str, Any]) -> set[str]:
    types: set[str] = set()
    for prov in schema["provider_schemas"].values():
        types.update(prov.get("resource_schemas", {}))
    return types
