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
import hashlib
import json
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from fnmatch import fnmatch
from pathlib import Path
from typing import Any

from .controller import CollectionObservation, Controller
from .errors import ControllerResponseError, ExternalDocumentError
from .manifest import CLASSIFIED_SECTIONS, MANIFEST, PROBE_ENDPOINTS, ResourceSpec
from .values import FrozenObject, FrozenValue, freeze_value

_CONTROLLER_IDENTIFIER = re.compile(r"^[A-Za-z0-9_.:-]{1,120}$")


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


def _sorted_lines(findings: Iterable[Finding]) -> list[str]:
    return [f.line() for f in
            sorted(findings, key=lambda f: (f.kind, f.identifier, f.detail))]


@dataclass(frozen=True)
class CoverageReport:
    gaps: tuple[Finding, ...] = field(default_factory=tuple)
    accepted: tuple[Finding, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        object.__setattr__(self, "gaps", tuple(self.gaps))
        object.__setattr__(self, "accepted", tuple(self.accepted))

    def gap_lines(self) -> list[str]:
        return _sorted_lines(self.gaps)


@dataclass(frozen=True)
class CoverageSnapshot:
    """One immutable collection window used by policy and receipt identity."""

    observations: tuple[CollectionObservation, ...]

    def __post_init__(self) -> None:
        ordered = tuple(sorted(self.observations, key=lambda item: item.endpoint_id))
        if len({item.endpoint_id for item in ordered}) != len(ordered):
            raise ValueError("duplicate coverage endpoint")
        object.__setattr__(self, "observations", ordered)

    def observation(self, endpoint: str) -> CollectionObservation:
        for observation in self.observations:
            if observation.endpoint_id == endpoint:
                return observation
        raise KeyError(endpoint)


@dataclass(frozen=True)
class CoverageSchema:
    """Only provider-schema facts consumed by coverage policy."""

    setting_sections: tuple[tuple[str, tuple[str, ...]], ...]
    resource_types: tuple[str, ...]

    def sections(self) -> dict[str, set[str]]:
        return {name: set(fields) for name, fields in self.setting_sections}


# unifi_setting attributes that are not controller sections.
_NON_SECTION_ATTRS = frozenset({"site", "id", "timeouts"})


def setting_schema_sections(schema: dict[str, Any]) -> dict[str, set[str]]:
    """Map unifi_setting attribute name -> normalized nested-field names.

    Reads the resource out of `tofu providers schema -json`. Raises KeyError
    when no provider in the schema defines unifi_setting — the audit must
    never run blind (a missing schema would reintroduce silent ignoring).
    """
    return parse_coverage_schema(schema).sections()


def schema_resource_types(schema: dict[str, Any]) -> set[str]:
    return set(parse_coverage_schema(schema).resource_types)


def parse_coverage_schema(schema: object) -> CoverageSchema:
    """Validate and retain exactly the provider facts coverage consumes."""
    try:
        freeze_value(schema)
        document = _schema_mapping(schema)
        providers = _schema_mapping(document.get("provider_schemas"))
        resource_types: set[str] = set()
        setting_sections: tuple[tuple[str, tuple[str, ...]], ...] | None = None
        for provider_name, provider_value in providers.items():
            _validate_schema_key(provider_name, allow_slash=True)
            provider = _schema_mapping(provider_value)
            resources = _schema_mapping(provider.get("resource_schemas", {}))
            for resource_name, resource_value in resources.items():
                _validate_schema_key(resource_name)
                resource = _schema_mapping(resource_value)
                resource_types.add(resource_name)
                if resource_name != "unifi_setting":
                    continue
                parsed_sections = _parse_setting_sections(resource)
                if setting_sections is not None and setting_sections != parsed_sections:
                    raise ValueError("conflicting unifi_setting schemas")
                setting_sections = parsed_sections
        if setting_sections is None:
            raise KeyError("unifi_setting not found in provider schema")
        return CoverageSchema(setting_sections, tuple(sorted(resource_types)))
    except KeyError:
        raise
    except (AttributeError, TypeError, ValueError) as exc:
        raise ExternalDocumentError(
            "provider_schema", "coverage", "invalid document"
        ) from exc


def _parse_setting_sections(
    resource: dict[str, object],
) -> tuple[tuple[str, tuple[str, ...]], ...]:
    block = _schema_mapping(resource.get("block"))
    attributes = _schema_mapping(block.get("attributes"))
    sections: list[tuple[str, tuple[str, ...]]] = []
    for name, spec_value in attributes.items():
        _validate_schema_key(name)
        spec = _schema_mapping(spec_value)
        nested_value = spec.get("nested_type")
        fields: tuple[str, ...] = ()
        if nested_value is not None:
            nested = _schema_mapping(nested_value)
            nested_attributes = _schema_mapping(nested.get("attributes"))
            normalized: list[str] = []
            for field, field_spec in nested_attributes.items():
                _validate_schema_key(field)
                _schema_mapping(field_spec)
                normalized.append(_norm(field))
            fields = tuple(sorted(normalized))
        if name not in _NON_SECTION_ATTRS:
            sections.append((name, fields))
    return tuple(sorted(sections))


def _schema_mapping(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise ValueError("provider schema value is not an object")
    return value


def _validate_schema_key(value: str, *, allow_slash: bool = False) -> None:
    if (
        not value
        or len(value) > 240
        or not value.isprintable()
        or (not allow_slash and _CONTROLLER_IDENTIFIER.fullmatch(value) is None)
    ):
        raise ValueError("invalid provider schema key")


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
        section_value = record.get("key")
        if (
            not isinstance(section_value, str)
            or _CONTROLLER_IDENTIFIER.fullmatch(section_value) is None
        ):
            raise ValueError("invalid controller setting key")
        section = section_value
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
    accepted, and controller-policy absence -> accepted. Other endpoint
    failures remain operational errors. Endpoints claimed by a MANIFEST spec
    are skipped — they are managed, not probed.
    """
    mapped = {s.endpoint for s in manifest}
    observations: list[CollectionObservation] = []
    for endpoint in sorted(PROBE_ENDPOINTS):
        if endpoint not in mapped:
            observations.append(ctl.collection_observation(endpoint))
    for observation in observations:
        _validate_coverage_observation(observation)
    return _audit_endpoint_observations(tuple(observations))


def _audit_endpoint_observations(
    observations: tuple[CollectionObservation, ...],
) -> tuple[list[Finding], list[Finding]]:
    """Interpret immutable endpoint observations without performing I/O."""
    gaps: list[Finding] = []
    accepted: list[Finding] = []
    for observation in observations:
        endpoint = observation.endpoint_id
        label = PROBE_ENDPOINTS[endpoint]
        if observation.policy_absent:
            accepted.append(Finding("endpoint", endpoint, "absent by controller policy"))
            continue
        objs = _records(observation)
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
    schema: dict[str, Any] | CoverageSchema,
    manifest: Iterable[ResourceSpec] = MANIFEST,
) -> list[Finding]:
    """Inverse check: provider resources ubitofu's MANIFEST does not map.

    Catches ubitofu falling behind its own provider (a resource shipped in
    the fork with no ResourceSpec — e.g. unifi_ap_group before its entry
    lands).
    """
    manifest_types = {s.resource_type for s in manifest}
    resource_types = (
        set(schema.resource_types)
        if isinstance(schema, CoverageSchema)
        else schema_resource_types(schema)
    )
    return [Finding("resource", rtype,
                    "provider supports it; ubitofu MANIFEST does not map it")
            for rtype in sorted(resource_types - manifest_types)]


def audit_guest_networks(ctl: Controller) -> list[Finding]:
    """Temporary: guest networks are excluded by the unifi_network
    discriminator (adoption needs ZBF zone-coupling work, tracked
    separately). Reported here so the exclusion is never silent; delete this
    check when the discriminator gains `guest`.
    """
    observation = ctl.collection_observation("rest/networkconf")
    _validate_coverage_observation(observation)
    return _audit_guest_network_records(_records(observation))


def _audit_guest_network_records(records: list[dict[str, object]]) -> list[Finding]:
    n = sum(1 for net in records if net.get("purpose") == "guest")
    if not n:
        return []
    return [Finding(
        "object", "unifi_network",
        f"{n} guest network(s) excluded by discriminator "
        "(guest adoption pending)")]


def collect_coverage_snapshot(
    ctl: Controller, manifest: Iterable[ResourceSpec] = MANIFEST
) -> CoverageSnapshot:
    """Read each endpoint once while preserving typed policy absence."""
    mapped = {spec.endpoint for spec in manifest}
    endpoints = {"get/setting", "rest/networkconf"}
    endpoints.update(endpoint for endpoint in PROBE_ENDPOINTS if endpoint not in mapped)
    return CoverageSnapshot(
        tuple(ctl.collection_observation(endpoint) for endpoint in sorted(endpoints))
    )


def audit_coverage_snapshot(
    snapshot: CoverageSnapshot,
    schema: dict[str, Any] | CoverageSchema,
) -> CoverageReport:
    """Interpret one captured window without rereading controller endpoints."""
    _validate_coverage_snapshot(snapshot)
    parsed_schema = schema if isinstance(schema, CoverageSchema) else parse_coverage_schema(schema)
    s_gaps, s_accepted = audit_settings(
        _records(snapshot.observation("get/setting")), parsed_schema.sections()
    )
    endpoint_observations = tuple(
        observation
        for observation in snapshot.observations
        if observation.endpoint_id in PROBE_ENDPOINTS
    )
    e_gaps, e_accepted = _audit_endpoint_observations(endpoint_observations)
    guest_gaps = _audit_guest_network_records(
        _records(snapshot.observation("rest/networkconf"))
    )
    return CoverageReport(
        gaps=tuple(s_gaps + e_gaps + audit_manifest_lag(parsed_schema)
                   + guest_gaps),
        accepted=tuple(s_accepted + e_accepted),
    )


def _validate_coverage_snapshot(snapshot: CoverageSnapshot) -> None:
    for observation in snapshot.observations:
        _validate_coverage_observation(observation)


def _validate_coverage_observation(observation: CollectionObservation) -> None:
    records = _records(observation)
    if observation.endpoint_id == "get/setting":
        for record in records:
            key = record.get("key")
            if not isinstance(key, str) or _CONTROLLER_IDENTIFIER.fullmatch(key) is None:
                _invalid_controller_document(observation.endpoint_id)
            for field in record:
                if (
                    field not in _BOOKKEEPING
                    and _CONTROLLER_IDENTIFIER.fullmatch(field) is None
                ):
                    _invalid_controller_document(observation.endpoint_id)
    if observation.endpoint_id in PROBE_ENDPOINTS:
        for record in records:
            _validate_default_marker(
                record,
                "attr_no_delete",
                endpoint=observation.endpoint_id,
                string_allowed=False,
            )
            _validate_default_marker(
                record,
                "attr_hidden_id",
                endpoint=observation.endpoint_id,
                string_allowed=True,
            )
    if observation.endpoint_id == "rest/networkconf":
        for record in records:
            purpose = record.get("purpose")
            if purpose is not None and (
                not isinstance(purpose, str)
                or _CONTROLLER_IDENTIFIER.fullmatch(purpose) is None
            ):
                _invalid_controller_document(observation.endpoint_id)


def _validate_default_marker(
    record: dict[str, object],
    field: str,
    *,
    endpoint: str,
    string_allowed: bool,
) -> None:
    if field not in record:
        return
    value = record[field]
    if isinstance(value, bool):
        return
    if (
        string_allowed
        and isinstance(value, str)
        and _CONTROLLER_IDENTIFIER.fullmatch(value) is not None
    ):
        return
    _invalid_controller_document(endpoint)


def _invalid_controller_document(endpoint: str) -> None:
    raise ControllerResponseError(endpoint, 200, "invalid document")


def digest_coverage_report(report: CoverageReport) -> str:
    """Digest the non-secret policy projection, never raw controller values."""
    rows = sorted(
        [
            {
                "bucket": bucket,
                "kind": finding.kind,
                "identifier": finding.identifier,
                "detail": finding.detail,
            }
            for bucket, findings in (("gap", report.gaps), ("accepted", report.accepted))
            for finding in findings
        ],
        key=lambda row: (
            row["bucket"], row["kind"], row["identifier"], row["detail"]
        ),
    )
    raw = json.dumps(rows, sort_keys=True, separators=(",", ":")).encode("ascii")
    domain = b"dev.ubitofu.coverage-policy-projection.v1"
    digest = hashlib.sha256()
    for value in (domain, raw):
        digest.update(len(value).to_bytes(8, "big"))
        digest.update(value)
    return digest.hexdigest()


def digest_coverage_schema(schema: CoverageSchema) -> str:
    """Digest the consumed schema projection without descriptions or extensions."""
    projection = {
        "resource_types": list(schema.resource_types),
        "setting_sections": [
            [name, list(fields)] for name, fields in schema.setting_sections
        ],
    }
    raw = json.dumps(projection, sort_keys=True, separators=(",", ":")).encode("ascii")
    domain = b"dev.ubitofu.coverage-schema-projection.v1"
    digest = hashlib.sha256()
    for value in (domain, raw):
        digest.update(len(value).to_bytes(8, "big"))
        digest.update(value)
    return digest.hexdigest()


def audit(ctl: Controller, schema: dict[str, Any]) -> CoverageReport:
    """Run canonical coverage collection and policy for legacy internal callers."""
    return audit_coverage_snapshot(collect_coverage_snapshot(ctl), schema)


def _records(observation: CollectionObservation) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    for record in observation.records:
        thawed = _thaw(record)
        if not isinstance(thawed, dict):
            raise ValueError("collection record is not an object")
        records.append(thawed)
    return records


def _thaw(value: FrozenValue) -> object:
    if value is None or isinstance(value, bool | int | float | str):
        return value
    if isinstance(value, FrozenObject):
        return {key: _thaw(item) for key, item in value.items}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    raise ValueError("unsupported frozen value")


_COVERAGE_HEADER = """\
# Provider coverage

Generated by `ubitofu` — do not edit; every run rewrites this file.
A new gap line arrives in a drift PR; merging that PR is the acceptance act.
Gaps close via provider PRs (settable attributes for real config,
computed + sensitive for controller internals), never via ignore lists.
"""


def render_coverage_md(report: CoverageReport) -> str:
    """Render a byte-stable COVERAGE.md from a CoverageReport."""
    def block(title: str, findings: Iterable[Finding]) -> str:
        lines = _sorted_lines(findings)
        body = "\n".join(f"- {ln}" for ln in lines) if lines else "None."
        return f"## {title}\n\n{body}\n"

    return (f"{_COVERAGE_HEADER}\n{block('Gaps', report.gaps)}\n"
            f"{block('Accepted', report.accepted)}")


def write_coverage_md(workdir: Path, report: CoverageReport) -> None:
    """Write a COVERAGE.md file to the given directory."""
    (workdir / "COVERAGE.md").write_text(render_coverage_md(report))
