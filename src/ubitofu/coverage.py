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
from collections.abc import Iterable
from dataclasses import dataclass, field
from fnmatch import fnmatch
from pathlib import Path
from typing import Any

import httpx

from .controller import Controller
from .manifest import CLASSIFIED_SECTIONS, MANIFEST, PROBE_ENDPOINTS, ResourceSpec


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


# Live get/setting records carry these controller bookkeeping keys in every
# section; they are not config and never count as fields.
_BOOKKEEPING = frozenset(
    {"_id", "key", "site_id", "attr_hidden_id", "attr_no_delete", "attr_no_edit"})

# Live section key -> unifi_setting attribute, where the names differ.
_LIVE_TO_SCHEMA = {"rsyslogd": "syslog", "ips_suppression": "ips"}

# Live sections whose fields are folded into another schema attribute under a
# prefix: ips_suppression's `whitelist` is modeled as ips.suppression_whitelist.
_FIELD_PREFIXES = {"ips_suppression": "suppression_"}


def _classified_reason(section: str) -> str | None:
    for pattern, reason in CLASSIFIED_SECTIONS.items():
        if fnmatch(section, pattern):
            return reason
    return None


def audit_settings(
    live: list[dict[str, Any]],
    schema_sections: dict[str, set[str]],
) -> tuple[list[Finding], list[Finding]]:
    """Bucket every live setting section: gap, accepted, or field-checked.

    A live section is never dropped: empty bodies carry no config (nothing to
    manage), classified sections are accepted with their written reason, and
    everything else is either field-checked against the schema or reported as
    a section gap.
    """
    gaps: list[Finding] = []
    accepted: list[Finding] = []
    for record in live:
        section = str(record.get("key", ""))
        body = {k: v for k, v in record.items() if k not in _BOOKKEEPING}
        if not body:
            continue
        reason = _classified_reason(section)
        if reason is not None:
            accepted.append(Finding("section", section, reason))
            continue
        fields = schema_sections.get(_LIVE_TO_SCHEMA.get(section, section))
        if fields is None:
            gaps.append(Finding(
                "section", section,
                f"live config ({len(body)} field(s)); "
                "provider unifi_setting lacks it"))
            continue
        prefix = _FIELD_PREFIXES.get(section, "")
        for fname in sorted(body):
            if _norm(prefix + fname) not in fields:
                gaps.append(Finding(
                    "field", f"{section}.{fname}",
                    "live on controller; provider schema lacks it"))
    return gaps, accepted


def audit_endpoints(
    ctl: Controller, manifest: Iterable[ResourceSpec] = MANIFEST
) -> tuple[list[Finding], list[Finding]]:
    """Probe unmapped collections; populated ones are gaps, defaults accepted.

    Every probe outcome is recorded: populated -> gap, built-in defaults ->
    accepted, HTTP 4xx (endpoint absent on this controller version) ->
    accepted. Endpoints claimed by a MANIFEST spec are skipped — they are
    managed, not probed.
    """
    mapped = {s.endpoint for s in manifest}
    gaps: list[Finding] = []
    accepted: list[Finding] = []
    for endpoint, label in sorted(PROBE_ENDPOINTS.items()):
        if endpoint in mapped:
            continue
        try:
            objs = ctl.collection(endpoint)
        except httpx.HTTPStatusError as exc:
            accepted.append(Finding(
                "endpoint", endpoint,
                "not present on this controller "
                f"(HTTP {exc.response.status_code})"))
            continue
        real = [o for o in objs
                if not (o.get("attr_no_delete") or o.get("attr_hidden_id"))]
        defaults = len(objs) - len(real)
        if defaults:
            accepted.append(Finding(
                "endpoint", endpoint,
                f"{defaults} built-in default object(s) ({label}) "
                "— not manageable"))
        if real:
            gaps.append(Finding(
                "endpoint", endpoint,
                f"{len(real)} object(s) ({label}) with no provider resource"))
    return gaps, accepted


def audit_manifest_lag(
    schema: dict[str, Any], manifest: Iterable[ResourceSpec] = MANIFEST
) -> list[Finding]:
    """Inverse check: provider resources ubitofu's MANIFEST does not map.

    Catches ubitofu falling behind its own provider (a resource shipped in
    the fork with no ResourceSpec — e.g. unifi_ap_group before its entry
    lands).
    """
    manifest_types = {s.resource_type for s in manifest}
    return [Finding("resource", rtype,
                    "provider supports it; ubitofu MANIFEST does not map it")
            for rtype in sorted(schema_resource_types(schema) - manifest_types)]


def audit_guest_networks(ctl: Controller) -> list[Finding]:
    """Temporary: guest networks are excluded by the unifi_network
    discriminator (adoption needs ZBF zone-coupling work, tracked
    separately). Reported here so the exclusion is never silent; delete this
    check when the discriminator gains `guest`.
    """
    n = sum(1 for net in ctl.collection("rest/networkconf")
            if net.get("purpose") == "guest")
    if not n:
        return []
    return [Finding(
        "object", "unifi_network",
        f"{n} guest network(s) excluded by discriminator "
        "(guest adoption pending)")]


def audit(ctl: Controller, schema: dict[str, Any]) -> CoverageReport:
    """Run every coverage check against one controller snapshot + schema."""
    s_gaps, s_accepted = audit_settings(
        ctl.collection("get/setting"), setting_schema_sections(schema))
    e_gaps, e_accepted = audit_endpoints(ctl)
    return CoverageReport(
        gaps=(s_gaps + e_gaps + audit_manifest_lag(schema)
              + audit_guest_networks(ctl)),
        accepted=s_accepted + e_accepted,
    )


_COVERAGE_HEADER = """\
# Provider coverage

Generated by `ubitofu` — do not edit; every run rewrites this file.
A new gap line arrives in a drift PR; merging that PR is the acceptance act.
Gaps close via provider PRs (settable attributes for real config,
computed + sensitive for controller internals), never via ignore lists.
"""


def render_coverage_md(report: CoverageReport) -> str:
    """Render a byte-stable COVERAGE.md from a CoverageReport."""
    def block(title: str, findings: list[Finding]) -> str:
        lines = _sorted_lines(findings)
        body = "\n".join(f"- {ln}" for ln in lines) if lines else "None."
        return f"## {title}\n\n{body}\n"

    return (f"{_COVERAGE_HEADER}\n{block('Gaps', report.gaps)}\n"
            f"{block('Accepted', report.accepted)}")


def write_coverage_md(workdir: Path, report: CoverageReport) -> None:
    """Write a COVERAGE.md file to the given directory."""
    (workdir / "COVERAGE.md").write_text(render_coverage_md(report))
