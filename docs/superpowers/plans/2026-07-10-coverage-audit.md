# Schema-Driven Provider-Coverage Audit Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** ubitofu audits every live controller item (endpoint objects, setting sections, setting fields) against the provider's actual schema and reports coverage gaps in a committed `COVERAGE.md`, so no config is ever silently ignored.

**Architecture:** A new pure module `coverage.py` diffs the live controller snapshot against `tofu providers schema -json`. Its `CoverageReport` renders byte-stably into `COVERAGE.md` (written by reconcile/generate into the workdir) and into the console report. The three legacy gap mechanisms (`UNMAPPED_ENDPOINTS`, `_guest_network_gaps`, ad-hoc endpoint checks) fold into it. Spec: `docs/superpowers/specs/2026-07-10-coverage-audit-design.md`.

**Tech Stack:** Python 3.11+, httpx, pytest + hypothesis, OpenTofu CLI (read-only), mypy --strict, ruff.

## Global Constraints

- Every new file starts with the two-line header used repo-wide:
  `# SPDX-License-Identifier: GPL-3.0-or-later` / `# Copyright (C) 2026 James Braid`
- mypy strict passes: `uv run --extra dev mypy`
- ruff passes: `uv run --extra dev ruff check src tests` (line length 100, rules E,F,I,UP,B)
- Tests: `uv run --extra dev pytest` (addopts `-q` comes from pyproject)
- Plan-only invariant: coverage code performs **GET requests only** — never mutates the controller, never runs a forbidden tofu command (TofuRunner._guard enforces this; do not bypass it).
- Commit messages: kernel style — `subsystem: imperative summary`, lowercase, no trailing period, no conventional-commit prefixes (`feat:`/`fix:` are banned). Subsystems in this repo: `coverage`, `manifest`, `reconcile`, `cli`, `reporter`, `tests`, `release`.
- No timestamps, counters, or randomness in `COVERAGE.md` output — two runs against an unchanged controller must produce identical bytes.
- The four-bucket model from the spec: every live item is managed, gap, or accepted (classified). With the ignore lists removed, "unknown" collapses into "gap" — there is deliberately no code path that drops an item without reporting it.

---

### Task 1: Manifest inventories — `PROBE_ENDPOINTS` and `CLASSIFIED_SECTIONS`

**Files:**
- Modify: `src/ubitofu/manifest.py` (append after `MANIFEST`; do NOT delete `UNMAPPED_ENDPOINTS` yet — the enumerator still imports it until Task 8)
- Test: `tests/test_manifest.py` (append)

**Interfaces:**
- Consumes: `MANIFEST` (existing tuple of `ResourceSpec`)
- Produces: `PROBE_ENDPOINTS: dict[str, str]` (endpoint → human label) and `CLASSIFIED_SECTIONS: dict[str, str]` (fnmatch glob → reason), imported by `coverage.py` in Tasks 3–4.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_manifest.py`:

```python
from ubitofu.manifest import CLASSIFIED_SECTIONS, MANIFEST, PROBE_ENDPOINTS


def test_probe_endpoints_cover_the_audited_collections():
    # The probe universe from the 2026-07-10 UDM audit. Endpoints later
    # claimed by a MANIFEST spec are skipped at runtime, so overlap with
    # MANIFEST is legal here — but today these must all be probe-only.
    for ep in (
        "v2/api/site/{site}/nat",
        "v2/api/site/{site}/content-filtering",
        "v2/api/site/{site}/apgroups",
        "v2/api/site/{site}/trafficrules",
        "v2/api/site/{site}/qos-rules",
        "v2/api/site/{site}/acl-rules",
        "v2/api/site/{site}/wan-slas",
        "v2/api/site/{site}/device-tags",
        "rest/scheduletask",
        "rest/dpigroup",
        "rest/dpiapp",
        "rest/wlangroup",
        "rest/hotspotop",
        "rest/hotspotpackage",
        "rest/hotspot2conf",
        "rest/channelplan",
    ):
        assert ep in PROBE_ENDPOINTS
        assert PROBE_ENDPOINTS[ep]  # non-empty label


def test_probe_endpoints_are_currently_unmapped():
    mapped = {s.endpoint for s in MANIFEST}
    assert not mapped & set(PROBE_ENDPOINTS)


def test_classified_sections_is_deliberately_tiny():
    # Acceptance lives in git (COVERAGE.md merges), not in code. Only
    # structurally-unclosable entries belong here. Growing this dict is a
    # design decision — the test forces that conversation.
    assert set(CLASSIFIED_SECTIONS) == {"super_*"}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run --extra dev pytest tests/test_manifest.py -q`
Expected: FAIL with `ImportError: cannot import name 'CLASSIFIED_SECTIONS'`

- [ ] **Step 3: Implement**

Append to `src/ubitofu/manifest.py` (after `UNMAPPED_ENDPOINTS`):

```python
# Endpoints probed by the coverage audit (coverage.py) beyond those MANIFEST
# maps. A populated, unmapped collection is a coverage gap; built-in defaults
# (attr_no_delete / attr_hidden_id) are accepted. An endpoint that later gains
# a MANIFEST spec is skipped automatically — mapped endpoints are derived from
# MANIFEST at runtime, never repeated here.
PROBE_ENDPOINTS: dict[str, str] = {
    "v2/api/site/{site}/nat": "NAT rules",
    "v2/api/site/{site}/content-filtering": "DNS content-filtering",
    "v2/api/site/{site}/apgroups": "AP groups",
    "v2/api/site/{site}/trafficrules": "traffic rules",
    "v2/api/site/{site}/qos-rules": "QoS rules",
    "v2/api/site/{site}/acl-rules": "switch ACL rules",
    "v2/api/site/{site}/wan-slas": "WAN SLA monitors",
    "v2/api/site/{site}/device-tags": "device tags",
    "rest/scheduletask": "scheduled tasks",
    "rest/dpigroup": "DPI groups",
    "rest/dpiapp": "DPI app rules",
    "rest/wlangroup": "WLAN groups (legacy)",
    "rest/hotspotop": "hotspot operators",
    "rest/hotspotpackage": "hotspot packages",
    "rest/hotspot2conf": "Hotspot 2.0 config",
    "rest/channelplan": "channel plans",
}

# Setting sections that are STRUCTURALLY out of scope — closable by neither a
# provider PR nor adoption. Deliberately tiny: everything else stays visible
# in COVERAGE.md until settled (acceptance = merging the PR that adds the
# line; silencing = a provider PR modeling the field, settable or
# computed+sensitive). Keys are fnmatch globs against the live section key.
CLASSIFIED_SECTIONS: dict[str, str] = {
    "super_*": "console-scope; a site-scoped unifi_setting cannot model it",
}
```

- [ ] **Step 4: Run tests, mypy, ruff**

Run: `uv run --extra dev pytest tests/test_manifest.py -q && uv run --extra dev mypy && uv run --extra dev ruff check src tests`
Expected: all PASS/clean

- [ ] **Step 5: Commit**

```bash
git add src/ubitofu/manifest.py tests/test_manifest.py
git commit -m "manifest: add coverage probe universe and classified sections"
```

---

### Task 2: `coverage.py` — findings model and provider-schema introspection

**Files:**
- Create: `src/ubitofu/coverage.py`
- Create: `tests/fixtures/coverage/providers_schema.json`
- Test: `tests/test_coverage.py` (create)

**Interfaces:**
- Consumes: `tofu providers schema -json` output shape — `{"provider_schemas": {"<addr>": {"resource_schemas": {"<type>": {"block": {"attributes": {...}}}}}}}`. Nested (plugin-framework) attributes carry `"nested_type": {"attributes": {...}}`.
- Produces (used by Tasks 3–7):
  - `Finding(kind: str, identifier: str, detail: str)` frozen dataclass with `line() -> str`
  - `CoverageReport(gaps: list[Finding], accepted: list[Finding])` with `gap_lines() -> list[str]`
  - `setting_schema_sections(schema: dict[str, Any]) -> dict[str, set[str]]` — schema attr name → normalized nested-field names
  - `schema_resource_types(schema: dict[str, Any]) -> set[str]`
  - `_norm(name: str) -> str`

- [ ] **Step 1: Write the schema fixture**

Create `tests/fixtures/coverage/providers_schema.json`. A trimmed but shape-faithful providers-schema: `unifi_setting` with three sections (`mgmt`, `dpi`, `syslog`), plus resource types `unifi_network` and `unifi_ap_group`:

```json
{
  "format_version": "1.0",
  "provider_schemas": {
    "registry.terraform.io/jamesbraid/unifi": {
      "resource_schemas": {
        "unifi_setting": {
          "block": {
            "attributes": {
              "site": {"type": "string", "optional": true},
              "id": {"type": "string", "computed": true},
              "timeouts": {"nested_type": {"attributes": {"read": {"type": "string"}}}},
              "mgmt": {
                "nested_type": {
                  "attributes": {
                    "led_enabled": {"type": "bool", "optional": true},
                    "ssh_enabled": {"type": "bool", "optional": true}
                  }
                }
              },
              "dpi": {
                "nested_type": {
                  "attributes": {
                    "enabled": {"type": "bool", "optional": true},
                    "fingerprinting_enabled": {"type": "bool", "optional": true}
                  }
                }
              },
              "syslog": {
                "nested_type": {
                  "attributes": {
                    "enabled": {"type": "bool", "optional": true},
                    "ip": {"type": "string", "optional": true}
                  }
                }
              },
              "ips": {
                "nested_type": {
                  "attributes": {
                    "enabled_categories": {"type": ["list", "string"], "optional": true},
                    "suppression_alerts": {"nested_type": {"attributes": {}}},
                    "suppression_whitelist": {"nested_type": {"attributes": {}}}
                  }
                }
              }
            }
          }
        },
        "unifi_network": {"block": {"attributes": {"name": {"type": "string"}}}},
        "unifi_ap_group": {"block": {"attributes": {"name": {"type": "string"}}}}
      }
    }
  }
}
```

- [ ] **Step 2: Write the failing tests**

Create `tests/test_coverage.py`:

```python
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 James Braid
import json

import pytest

from ubitofu.coverage import (
    Finding,
    _norm,
    schema_resource_types,
    setting_schema_sections,
)


@pytest.fixture
def schema(fixtures_dir):
    return json.loads(
        (fixtures_dir / "coverage" / "providers_schema.json").read_text())


def test_norm_matches_camelcase_to_snake():
    assert _norm("fingerprintingEnabled") == _norm("fingerprinting_enabled")


def test_setting_schema_sections_extracts_nested_fields(schema):
    sections = setting_schema_sections(schema)
    assert sections["mgmt"] == {_norm("led_enabled"), _norm("ssh_enabled")}
    assert _norm("fingerprintingEnabled") in sections["dpi"]


def test_setting_schema_sections_excludes_non_sections(schema):
    sections = setting_schema_sections(schema)
    for meta in ("site", "id", "timeouts"):
        assert meta not in sections


def test_setting_schema_sections_raises_without_unifi_setting():
    with pytest.raises(KeyError):
        setting_schema_sections({"provider_schemas": {}})


def test_schema_resource_types(schema):
    assert schema_resource_types(schema) == {
        "unifi_setting", "unifi_network", "unifi_ap_group"}


def test_finding_line_is_deterministic():
    f = Finding("section", "mdns", "live config; provider unifi_setting lacks it")
    assert f.line() == "section mdns: live config; provider unifi_setting lacks it"
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `uv run --extra dev pytest tests/test_coverage.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'ubitofu.coverage'`

- [ ] **Step 4: Implement**

Create `src/ubitofu/coverage.py`:

```python
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
```

- [ ] **Step 5: Run tests, mypy, ruff**

Run: `uv run --extra dev pytest tests/test_coverage.py -q && uv run --extra dev mypy && uv run --extra dev ruff check src tests`
Expected: all PASS/clean

- [ ] **Step 6: Commit**

```bash
git add src/ubitofu/coverage.py tests/test_coverage.py tests/fixtures/coverage/providers_schema.json
git commit -m "coverage: add findings model and provider-schema introspection"
```

---

### Task 3: `coverage.py` — settings audit (sections + fields)

**Files:**
- Modify: `src/ubitofu/coverage.py`
- Test: `tests/test_coverage.py` (append)

**Interfaces:**
- Consumes: `setting_schema_sections()` output, `CLASSIFIED_SECTIONS` from manifest, live `get/setting` records (list of dicts, each with a `"key"` and its config fields; controller bookkeeping keys `_id`/`key`/`site_id`/`attr_*` present on every record).
- Produces: `audit_settings(live: list[dict[str, Any]], schema_sections: dict[str, set[str]]) -> tuple[list[Finding], list[Finding]]` — `(gaps, accepted)`.
- Quirks encoded here (from the live-UDM audit — do not simplify away):
  - live key `rsyslogd` ↔ schema attr `syslog`
  - live key `ips_suppression` maps into schema attr `ips`, with its fields prefixed `suppression_` (`whitelist` → `suppression_whitelist`, `alerts` → `suppression_alerts`)
  - a section whose body is empty after stripping bookkeeping (e.g. live `super_cloudaccess` is `{}`) carries no config and is not reported

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_coverage.py`:

```python
from ubitofu.coverage import audit_settings


def _sections(schema, fixtures_dir=None):
    return setting_schema_sections(schema)


def _settings_record(key, **body):
    return {"key": key, "_id": "x", "site_id": "s", **body}


def test_uncovered_section_with_config_is_a_gap(schema):
    gaps, accepted = audit_settings(
        [_settings_record("mdns", enabled_for="some")], _sections(schema))
    assert [f.identifier for f in gaps] == ["mdns"]
    assert gaps[0].kind == "section"
    assert not accepted


def test_covered_section_with_known_fields_is_clean(schema):
    gaps, accepted = audit_settings(
        [_settings_record("mgmt", led_enabled=True)], _sections(schema))
    assert not gaps and not accepted


def test_unknown_field_in_covered_section_is_a_field_gap(schema):
    gaps, _ = audit_settings(
        [_settings_record("mgmt", led_enabled=True, x_mgmt_key="REDACTED")],
        _sections(schema))
    assert [f.identifier for f in gaps] == ["mgmt.x_mgmt_key"]
    assert gaps[0].kind == "field"


def test_camelcase_live_field_matches_snake_schema(schema):
    gaps, _ = audit_settings(
        [_settings_record("dpi", fingerprintingEnabled=True)], _sections(schema))
    assert not gaps


def test_rsyslogd_alias_maps_to_syslog(schema):
    gaps, _ = audit_settings(
        [_settings_record("rsyslogd", enabled=True, ip="10.0.0.1")],
        _sections(schema))
    assert not gaps


def test_ips_suppression_maps_into_ips_with_prefix(schema):
    gaps, _ = audit_settings(
        [_settings_record("ips_suppression", whitelist=[], alerts=[])],
        _sections(schema))
    assert not gaps


def test_classified_section_is_accepted_with_reason(schema):
    gaps, accepted = audit_settings(
        [_settings_record("super_mgmt", autobackup_enabled=True)],
        _sections(schema))
    assert not gaps
    assert [f.identifier for f in accepted] == ["super_mgmt"]
    assert "console-scope" in accepted[0].detail


def test_empty_section_body_is_not_reported(schema):
    gaps, accepted = audit_settings(
        [_settings_record("super_cloudaccess")], _sections(schema))
    assert not gaps and not accepted
```

And the structural invariant (hypothesis), append:

```python
from hypothesis import given
from hypothesis import strategies as st

_key = st.text(alphabet="abcdefgh_", min_size=1, max_size=12)


@given(st.lists(
    st.tuples(_key, st.dictionaries(_key, st.booleans(), max_size=3)),
    max_size=8))
def test_every_live_section_lands_in_at_most_one_bucket(sections):
    live = [_settings_record(k, **body) for k, body in sections]
    schema_sections = {"mgmt": {_norm("led_enabled")}}
    gaps, accepted = audit_settings(live, schema_sections)
    gap_sections = {f.identifier for f in gaps if f.kind == "section"}
    accepted_sections = {f.identifier for f in accepted}
    assert not gap_sections & accepted_sections
    for k, body in sections:
        if not body:
            continue  # empty body: nothing to manage, legitimately absent
        in_schema = k in schema_sections  # covered -> field checks, no section line
        assert in_schema or (k in gap_sections) or (k in accepted_sections)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run --extra dev pytest tests/test_coverage.py -q`
Expected: FAIL with `ImportError: cannot import name 'audit_settings'`

- [ ] **Step 3: Implement**

Add to `src/ubitofu/coverage.py` (imports at top: `from fnmatch import fnmatch` and `from .manifest import CLASSIFIED_SECTIONS`):

```python
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
```

- [ ] **Step 4: Run tests, mypy, ruff**

Run: `uv run --extra dev pytest tests/test_coverage.py -q && uv run --extra dev mypy && uv run --extra dev ruff check src tests`
Expected: all PASS/clean

- [ ] **Step 5: Commit**

```bash
git add src/ubitofu/coverage.py tests/test_coverage.py
git commit -m "coverage: audit setting sections and fields against schema"
```

---

### Task 4: `coverage.py` — endpoint probe audit

**Files:**
- Modify: `src/ubitofu/coverage.py`
- Test: `tests/test_coverage.py` (append)

**Interfaces:**
- Consumes: `Controller.collection(endpoint)` (raises `httpx.HTTPStatusError` on 4xx — `Controller.get` calls `raise_for_status()`), `PROBE_ENDPOINTS`, `MANIFEST`.
- Produces: `audit_endpoints(ctl: Controller) -> tuple[list[Finding], list[Finding]]`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_coverage.py`. Reuse the `FakeController` pattern from `tests/test_enumerator.py`, extended with an HTTP-error map:

```python
import httpx

from ubitofu.controller import Controller
from ubitofu.coverage import audit_endpoints


class FakeCoverageController(Controller):
    def __init__(self, populated=None, errors=None):
        self.site = "default"
        self._populated = populated or {}   # endpoint -> list of objects
        self._errors = errors or {}         # endpoint -> HTTP status code

    def collection(self, endpoint):
        if endpoint in self._errors:
            code = self._errors[endpoint]
            raise httpx.HTTPStatusError(
                f"HTTP {code}",
                request=httpx.Request("GET", "https://unifi.example/x"),
                response=httpx.Response(code, request=httpx.Request(
                    "GET", "https://unifi.example/x")),
            )
        return self._populated.get(endpoint, [])


def test_populated_unmapped_endpoint_is_a_gap():
    ctl = FakeCoverageController(populated={
        "v2/api/site/{site}/nat": [{"_id": "n1", "type": "MASQUERADE"}]})
    gaps, accepted = audit_endpoints(ctl)
    nat = [f for f in gaps if f.identifier == "v2/api/site/{site}/nat"]
    assert len(nat) == 1
    assert "1 object(s)" in nat[0].detail and "NAT rules" in nat[0].detail


def test_default_objects_are_accepted_not_gaps():
    ctl = FakeCoverageController(populated={
        "rest/wlangroup": [
            {"_id": "a", "name": "Default", "attr_no_delete": True},
            {"_id": "b", "name": "Off", "attr_hidden_id": "Off"},
        ]})
    gaps, accepted = audit_endpoints(ctl)
    assert not [f for f in gaps if f.identifier == "rest/wlangroup"]
    wg = [f for f in accepted if f.identifier == "rest/wlangroup"]
    assert len(wg) == 1 and "2 built-in default object(s)" in wg[0].detail


def test_missing_endpoint_is_accepted_with_status():
    ctl = FakeCoverageController(errors={"rest/hotspot2conf": 404})
    gaps, accepted = audit_endpoints(ctl)
    h2 = [f for f in accepted if f.identifier == "rest/hotspot2conf"]
    assert len(h2) == 1 and "HTTP 404" in h2[0].detail
    assert not [f for f in gaps if f.identifier == "rest/hotspot2conf"]


def test_empty_endpoint_reports_nothing():
    gaps, accepted = audit_endpoints(FakeCoverageController())
    assert not gaps and not accepted


def test_manifest_mapped_endpoints_are_never_probed():
    # If a PROBE endpoint gains a MANIFEST spec, the probe must skip it.
    from ubitofu.manifest import ResourceSpec
    spec = ResourceSpec("unifi_nat_rule", "v2/api/site/{site}/nat", "_id")
    ctl = FakeCoverageController(populated={
        "v2/api/site/{site}/nat": [{"_id": "n1"}]})
    gaps, _ = audit_endpoints(ctl, manifest=(spec,))
    assert not [f for f in gaps if f.identifier == "v2/api/site/{site}/nat"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run --extra dev pytest tests/test_coverage.py -q`
Expected: FAIL with `ImportError: cannot import name 'audit_endpoints'`

- [ ] **Step 3: Implement**

Add to `src/ubitofu/coverage.py` (imports: `import httpx`, `from collections.abc import Iterable`, and extend the manifest import to `from .manifest import CLASSIFIED_SECTIONS, MANIFEST, PROBE_ENDPOINTS, ResourceSpec`; also `from .controller import Controller`):

```python
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
```

- [ ] **Step 4: Run tests, mypy, ruff**

Run: `uv run --extra dev pytest tests/test_coverage.py -q && uv run --extra dev mypy && uv run --extra dev ruff check src tests`
Expected: all PASS/clean

- [ ] **Step 5: Commit**

```bash
git add src/ubitofu/coverage.py tests/test_coverage.py
git commit -m "coverage: probe unmapped endpoints, accept defaults and 404s"
```

---

### Task 5: `coverage.py` — manifest-lag check, guest-network check, `audit()` orchestrator

**Files:**
- Modify: `src/ubitofu/coverage.py`
- Test: `tests/test_coverage.py` (append)

**Interfaces:**
- Consumes: everything from Tasks 2–4.
- Produces:
  - `audit_manifest_lag(schema: dict[str, Any], manifest: Iterable[ResourceSpec] = MANIFEST) -> list[Finding]`
  - `audit_guest_networks(ctl: Controller) -> list[Finding]`
  - `audit(ctl: Controller, schema: dict[str, Any]) -> CoverageReport` — the single entry point Tasks 6–8 wire into the pipelines.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_coverage.py`:

```python
from ubitofu.coverage import (
    CoverageReport,
    audit,
    audit_guest_networks,
    audit_manifest_lag,
)


def test_manifest_lag_flags_unmapped_provider_resources(schema):
    # Fixture schema has unifi_ap_group; MANIFEST does not map it (yet).
    findings = audit_manifest_lag(schema)
    assert any(f.identifier == "unifi_ap_group" and f.kind == "resource"
               for f in findings)
    # unifi_network IS in MANIFEST — never flagged.
    assert not any(f.identifier == "unifi_network" for f in findings)


def test_guest_networks_reported_while_discriminator_excludes_them():
    ctl = FakeCoverageController(populated={
        "rest/networkconf": [
            {"_id": "g1", "name": "guest", "purpose": "guest"},
            {"_id": "c1", "name": "lan", "purpose": "corporate"},
        ]})
    findings = audit_guest_networks(ctl)
    assert len(findings) == 1
    assert "1 guest network(s)" in findings[0].detail


def test_no_guest_networks_no_finding():
    ctl = FakeCoverageController(populated={
        "rest/networkconf": [{"_id": "c1", "purpose": "corporate"}]})
    assert audit_guest_networks(ctl) == []


def test_audit_combines_all_checks(schema):
    ctl = FakeCoverageController(populated={
        "get/setting": [_settings_record("mdns", enabled_for="some"),
                        _settings_record("super_mgmt", enable_analytics=True)],
        "v2/api/site/{site}/nat": [{"_id": "n1"}],
        "rest/networkconf": [{"_id": "g1", "purpose": "guest"}],
    })
    report = audit(ctl, schema)
    assert isinstance(report, CoverageReport)
    kinds = {(f.kind, f.identifier) for f in report.gaps}
    assert ("section", "mdns") in kinds
    assert ("endpoint", "v2/api/site/{site}/nat") in kinds
    assert ("resource", "unifi_ap_group") in kinds
    assert ("object", "unifi_network") in kinds
    assert [f.identifier for f in report.accepted] == ["super_mgmt"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run --extra dev pytest tests/test_coverage.py -q`
Expected: FAIL with `ImportError: cannot import name 'audit'`

- [ ] **Step 3: Implement**

Add to `src/ubitofu/coverage.py`:

```python
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
```

- [ ] **Step 4: Run tests, mypy, ruff**

Run: `uv run --extra dev pytest tests/test_coverage.py -q && uv run --extra dev mypy && uv run --extra dev ruff check src tests`
Expected: all PASS/clean

- [ ] **Step 5: Commit**

```bash
git add src/ubitofu/coverage.py tests/test_coverage.py
git commit -m "coverage: manifest-lag and guest checks, audit orchestrator"
```

---

### Task 6: Rendering — `COVERAGE.md` writer and console section

**Files:**
- Modify: `src/ubitofu/coverage.py`
- Modify: `src/ubitofu/reporter.py`
- Test: `tests/test_coverage.py`, `tests/test_reporter.py` (append)

**Interfaces:**
- Produces:
  - `render_coverage_md(report: CoverageReport) -> str` — byte-stable Markdown
  - `write_coverage_md(workdir: Path, report: CoverageReport) -> None` — writes `<workdir>/COVERAGE.md`
  - `reporter.format_coverage(gap_lines: list[str], accepted_count: int) -> str` — console section; Tasks 7–8 call it with `enum_result.gaps + report.gap_lines()`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_coverage.py`:

```python
import random

from ubitofu.coverage import render_coverage_md, write_coverage_md


def _report():
    return CoverageReport(
        gaps=[
            Finding("section", "mdns", "live config (2 field(s)); "
                    "provider unifi_setting lacks it"),
            Finding("endpoint", "v2/api/site/{site}/nat",
                    "1 object(s) (NAT rules) with no provider resource"),
        ],
        accepted=[Finding("section", "super_mgmt",
                          "console-scope; a site-scoped unifi_setting "
                          "cannot model it")],
    )


def test_render_coverage_md_golden():
    md = render_coverage_md(_report())
    assert md.startswith("# Provider coverage\n")
    assert "## Gaps" in md and "## Accepted" in md
    assert "- section mdns:" in md
    assert "- endpoint v2/api/site/{site}/nat:" in md
    assert "- section super_mgmt:" in md
    assert "do not edit" in md.lower()


def test_render_coverage_md_is_byte_stable_under_input_order():
    r1, r2 = _report(), _report()
    random.Random(1).shuffle(r2.gaps)
    random.Random(2).shuffle(r2.accepted)
    assert render_coverage_md(r1) == render_coverage_md(r2)


def test_render_coverage_md_empty_report():
    md = render_coverage_md(CoverageReport())
    assert "None." in md  # both sections render explicitly, never omitted


def test_write_coverage_md(tmp_path):
    write_coverage_md(tmp_path, _report())
    text = (tmp_path / "COVERAGE.md").read_text()
    assert text == render_coverage_md(_report())
```

Append to `tests/test_reporter.py`:

```python
from ubitofu.reporter import format_coverage


def test_format_coverage_merges_gap_lines_and_accepted_count():
    out = format_coverage(["1 guest network(s) — pending",
                           "section mdns: provider lacks it"], 3)
    assert "Coverage gaps:" in out
    assert "  - 1 guest network(s) — pending" in out
    assert "3 accepted item(s)" in out and "COVERAGE.md" in out


def test_format_coverage_clean():
    assert format_coverage([], 0) == "Coverage: no coverage gaps detected."
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run --extra dev pytest tests/test_coverage.py tests/test_reporter.py -q`
Expected: FAIL with `ImportError` (render_coverage_md / format_coverage)

- [ ] **Step 3: Implement**

Add to `src/ubitofu/coverage.py` (import `from pathlib import Path`):

```python
_COVERAGE_HEADER = """\
# Provider coverage

Generated by `ubitofu` — do not edit; every run rewrites this file.
A new gap line arrives in a drift PR; merging that PR is the acceptance act.
Gaps close via provider PRs (settable attributes for real config,
computed + sensitive for controller internals), never via ignore lists.
"""


def render_coverage_md(report: CoverageReport) -> str:
    def block(title: str, findings: list[Finding]) -> str:
        lines = _sorted_lines(findings)
        body = "\n".join(f"- {ln}" for ln in lines) if lines else "None."
        return f"## {title}\n\n{body}\n"

    return (f"{_COVERAGE_HEADER}\n{block('Gaps', report.gaps)}\n"
            f"{block('Accepted', report.accepted)}")


def write_coverage_md(workdir: Path, report: CoverageReport) -> None:
    (workdir / "COVERAGE.md").write_text(render_coverage_md(report))
```

Add to `src/ubitofu/reporter.py` (below `format_gaps`):

```python
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
```

- [ ] **Step 4: Run tests, mypy, ruff**

Run: `uv run --extra dev pytest tests/test_coverage.py tests/test_reporter.py -q && uv run --extra dev mypy && uv run --extra dev ruff check src tests`
Expected: all PASS/clean

- [ ] **Step 5: Commit**

```bash
git add src/ubitofu/coverage.py src/ubitofu/reporter.py tests/test_coverage.py tests/test_reporter.py
git commit -m "coverage: byte-stable COVERAGE.md renderer and console section"
```

---

### Task 7: Wire the audit into reconcile and generate

**Files:**
- Modify: `src/ubitofu/pipeline.py`
- Test: `tests/test_coverage.py` (append; plus extend one existing integration test)

**Interfaces:**
- Consumes: `audit()`, `write_coverage_md()`, `format_coverage()` (Tasks 5–6); existing `run_reconcile` / `run_generate` structure (both already hold `ctl`, `schema`, `workdir`, `res.gaps`, `out`).
- Produces: `_emit_coverage(ctl: Controller, schema: dict[str, Any], workdir: Path, enum_gaps: list[str], out: IO[str]) -> None` — the single seam both pipelines call.

- [ ] **Step 1: Write the failing test for the seam**

Append to `tests/test_coverage.py`:

```python
import io

from ubitofu.pipeline import _emit_coverage


def test_emit_coverage_writes_file_and_prints(schema, tmp_path):
    ctl = FakeCoverageController(populated={
        "get/setting": [_settings_record("mdns", enabled_for="some")]})
    buf = io.StringIO()
    _emit_coverage(ctl, schema, tmp_path, ["unifi_bgp skipped — not configured"],
                   buf)
    text = (tmp_path / "COVERAGE.md").read_text()
    assert "- section mdns:" in text
    printed = buf.getvalue()
    assert "unifi_bgp skipped" in printed  # enumeration gaps share the section
    assert "section mdns:" in printed


def test_emit_coverage_is_byte_stable_across_runs(schema, tmp_path):
    ctl = FakeCoverageController(populated={
        "get/setting": [_settings_record("mdns", enabled_for="some")]})
    _emit_coverage(ctl, schema, tmp_path, [], io.StringIO())
    first = (tmp_path / "COVERAGE.md").read_bytes()
    _emit_coverage(ctl, schema, tmp_path, [], io.StringIO())
    assert (tmp_path / "COVERAGE.md").read_bytes() == first
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run --extra dev pytest tests/test_coverage.py -q`
Expected: FAIL with `ImportError: cannot import name '_emit_coverage'`

- [ ] **Step 3: Implement the seam and wire both pipelines**

In `src/ubitofu/pipeline.py`:

Add imports: `from .coverage import audit, write_coverage_md` and extend the reporter import with `format_coverage`.

Add the seam (near `run_generate`):

```python
def _emit_coverage(
    ctl: Controller,
    schema: dict[str, Any],
    workdir: Path,
    enum_gaps: list[str],
    out: IO[str],
) -> None:
    """Audit provider coverage, persist COVERAGE.md, print the section.

    Called by reconcile and generate after the schema fetch. COVERAGE.md is
    the acceptance ledger: a changed file rides the nightly drift PR, so new
    gaps notify and closures are visible.
    """
    report = audit(ctl, schema)
    write_coverage_md(workdir, report)
    print(format_coverage(enum_gaps + report.gap_lines(),
                          len(report.accepted)), file=out)
```

In `run_generate`, replace the line `print(format_gaps(res.gaps), file=out)` with:

```python
    _emit_coverage(ctl, schema, workdir, res.gaps, out)
```

In `run_reconcile`, after the existing `print(format_reconcile(...), file=out)` call and before `return 0`, add:

```python
    _emit_coverage(ctl, schema, workdir, res.gaps, out)
```

- [ ] **Step 4: Extend one existing integration test**

In `tests/test_integration.py`, find the reconcile end-to-end test (it monkeypatches `Controller` and uses a `FakeRunner`). Make its fakes coverage-ready:

- `FakeRunner.providers_schema()` must return the Task 2 fixture:
  `json.loads((fixtures_dir / "coverage" / "providers_schema.json").read_text())` — if it already returns a schema fixture, keep it and adjust the assertion below to that fixture's contents.
- The fake controller must answer `"get/setting"` and every `PROBE_ENDPOINTS` entry with `[]` (the `FakeController.collection` pattern already returns `[]` for unknown endpoints).

Then add to the end of that test:

```python
    coverage_md = workdir / "COVERAGE.md"
    assert coverage_md.exists()
    assert coverage_md.read_text().startswith("# Provider coverage")
```

(Adapt the `workdir` variable name to the test's local name.)

- [ ] **Step 5: Run the full suite, mypy, ruff**

Run: `uv run --extra dev pytest -q && uv run --extra dev mypy && uv run --extra dev ruff check src tests`
Expected: all PASS/clean

- [ ] **Step 6: Commit**

```bash
git add src/ubitofu/pipeline.py tests/test_coverage.py tests/test_integration.py
git commit -m "reconcile: write COVERAGE.md and report coverage on every run"
```

---

### Task 8: Wire enumerate; retire the legacy gap mechanisms

**Files:**
- Modify: `src/ubitofu/cli.py`
- Modify: `src/ubitofu/enumerator.py` (delete `_coverage_gaps`, `_guest_network_gaps`, their call sites, and the `UNMAPPED_ENDPOINTS` import)
- Modify: `src/ubitofu/manifest.py` (delete `UNMAPPED_ENDPOINTS`)
- Test: `tests/test_cli.py`, `tests/test_enumerator.py`, `tests/test_manifest.py`

**Interfaces:**
- Consumes: `audit()`, `format_coverage()`, `TofuRunner.providers_schema()`, `TofuError`.
- Produces: `cmd_enumerate` now requires a tofu-init'd `cfg.workdir` and prints the coverage section. `EnumerationResult.gaps` shrinks to per-object skips + the bgp singleton line — everything else comes from `coverage.audit()`.

- [ ] **Step 1: Write the failing CLI tests**

Append to `tests/test_cli.py`:

```python
def test_enumerate_errors_actionably_without_init(monkeypatch, fixtures_dir, capsys):
    import ubitofu.cli as climod
    from ubitofu.tofu_runner import TofuError

    monkeypatch.setattr(climod, "_controller", lambda cfg: object())

    class FailingRunner:
        def __init__(self, workdir):
            pass

        def providers_schema(self):
            raise TofuError("no schema available")

    monkeypatch.setattr(climod, "TofuRunner", FailingRunner)
    rc = main(["enumerate", "--config", str(fixtures_dir / "config.toml")])
    assert rc == 2
    err = capsys.readouterr().err
    assert "tofu init" in err  # actionable: no degraded silent mode
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run --extra dev pytest tests/test_cli.py -q`
Expected: FAIL — `rc == 0` today (enumerate never touches tofu), or missing `tofu init` text.

- [ ] **Step 3: Rewrite `cmd_enumerate`**

In `src/ubitofu/cli.py` add imports:

```python
from pathlib import Path

from .coverage import audit
from .reporter import format_coverage
from .tofu_runner import TofuError, TofuRunner
```

(Replace the existing `from .reporter import format_gaps` and `from .tofu_runner import TofuError` imports; `format_gaps` is no longer used here.)

Replace `cmd_enumerate`:

```python
def cmd_enumerate(cfg: Config, mode: str, out: IO[str]) -> int:
    ctl = _controller(cfg)
    runner = TofuRunner(workdir=Path(cfg.workdir))
    try:
        schema = runner.providers_schema()
    except TofuError as exc:
        raise TofuError(
            f"{exc}\nenumerate needs the provider schema for the coverage "
            f"audit: run `tofu init` in {cfg.workdir}") from exc
    res = enumerate_controller(ctl)
    report = audit(ctl, schema)
    print(emit_import_blocks(res.targets), file=out)
    print(format_coverage(res.gaps + report.gap_lines(),
                          len(report.accepted)), file=out)
    return 0
```

- [ ] **Step 4: Retire the legacy mechanisms**

In `src/ubitofu/enumerator.py`:
- Change the manifest import to `from .manifest import MANIFEST, ResourceSpec` (drop `UNMAPPED_ENDPOINTS`).
- Delete the functions `_coverage_gaps` and `_guest_network_gaps` entirely.
- In `enumerate_controller`, delete the two lines
  `result.gaps.extend(_guest_network_gaps(ctl, specs))` and
  `result.gaps.extend(_coverage_gaps(ctl))`.

In `src/ubitofu/manifest.py`: delete the `UNMAPPED_ENDPOINTS` dict and its comment (its three entries already live in `PROBE_ENDPOINTS` since Task 1).

- [ ] **Step 5: Update tests that referenced the deleted mechanisms**

Run: `uv run --extra dev pytest -q` and fix the failures, which are confined to:
- `tests/test_enumerator.py` — any test asserting guest/unmapped lines in `res.gaps` (e.g. the guest-gap assertion in the networkconf test): the *target* assertions stay (guest still not imported — the discriminator is unchanged); delete only the `res.gaps` assertions about guest/unmapped endpoints. Equivalent coverage now lives in `tests/test_coverage.py` (Tasks 4–5).
- `tests/test_manifest.py` — any test importing `UNMAPPED_ENDPOINTS`: delete it (superseded by the Task 1 probe tests).

Do not delete assertions about `_skip_reason` gap lines (app policy, radius default, usergroup default, bgp skipped) — those still flow through `EnumerationResult.gaps`.

- [ ] **Step 6: Run the full suite, mypy, ruff**

Run: `uv run --extra dev pytest -q && uv run --extra dev mypy && uv run --extra dev ruff check src tests`
Expected: all PASS/clean

- [ ] **Step 7: Commit**

```bash
git add src/ubitofu/cli.py src/ubitofu/enumerator.py src/ubitofu/manifest.py tests/
git commit -m "cli: audit coverage in enumerate, retire ad-hoc gap checks"
```

---

### Task 9: Real-UDM regression fixture

**Files:**
- Create: `tests/fixtures/coverage/settings_live.json`
- Test: `tests/test_coverage.py` (append)

**Interfaces:**
- Consumes: `audit_settings()`, `setting_schema_sections()`, the Task 2 schema fixture.
- Produces: a golden test pinning the section-gap set observed on the audited UDM (2026-07-10). When a provider PR adds a section, this test's expected set shrinks — that edit is the visible, reviewed coverage change.

- [ ] **Step 1: Write the sanitized live-settings fixture**

Create `tests/fixtures/coverage/settings_live.json`. One record per live section from the 2026-07-10 audit; bodies are truncated to 1–2 representative fields; ALL values sanitized (`"REDACTED"` for anything secret-shaped, booleans/labels kept). The section *names* are the data under test:

```json
[
  {"key": "auto_speedtest", "_id": "x", "site_id": "s", "enabled": true},
  {"key": "connectivity", "_id": "x", "site_id": "s", "uplink_type": "gateway", "x_mesh_psk": "REDACTED"},
  {"key": "country", "_id": "x", "site_id": "s", "code": "124"},
  {"key": "dashboard", "_id": "x", "site_id": "s", "layout_preference": "auto"},
  {"key": "doh", "_id": "x", "site_id": "s", "state": "auto"},
  {"key": "dpi", "_id": "x", "site_id": "s", "enabled": true, "fingerprintingEnabled": true},
  {"key": "element_adopt", "_id": "x", "site_id": "s", "enabled": true, "x_element_psk": "REDACTED"},
  {"key": "ether_lighting", "_id": "x", "site_id": "s", "network_defaults": []},
  {"key": "global_nat", "_id": "x", "site_id": "s", "mode": "auto"},
  {"key": "global_network", "_id": "x", "site_id": "s", "default_security_posture": "ALLOW_ALL"},
  {"key": "global_switch", "_id": "x", "site_id": "s", "stp_version": "rstp", "dhcp_snoop": true},
  {"key": "guest_access", "_id": "x", "site_id": "s", "portal_enabled": false, "auth": "none"},
  {"key": "igmp_snooping", "_id": "x", "site_id": "s", "enabled": false},
  {"key": "ips", "_id": "x", "site_id": "s", "enabled_categories": []},
  {"key": "ips_suppression", "_id": "x", "site_id": "s", "whitelist": [], "alerts": []},
  {"key": "ipsec", "_id": "x", "site_id": "s", "ikev2_reauthentication_method": "make-before-break"},
  {"key": "lcm", "_id": "x", "site_id": "s", "enabled": true},
  {"key": "locale", "_id": "x", "site_id": "s", "timezone": "Etc/UTC"},
  {"key": "magic_site_to_site_vpn", "_id": "x", "site_id": "s", "enabled": true, "x_private_key": "REDACTED"},
  {"key": "mdns", "_id": "x", "site_id": "s", "enabled_for": "some"},
  {"key": "mgmt", "_id": "x", "site_id": "s", "led_enabled": true, "x_mgmt_key": "REDACTED"},
  {"key": "netflow", "_id": "x", "site_id": "s", "enabled": false, "port": 2055},
  {"key": "network_optimization", "_id": "x", "site_id": "s", "enabled": true},
  {"key": "ntp", "_id": "x", "site_id": "s", "ntp_server_1": "time.google.com"},
  {"key": "openvpn", "_id": "x", "site_id": "s", "x_pregenerated_dh_key": "REDACTED"},
  {"key": "peer_to_peer", "_id": "x", "site_id": "s", "psk": "REDACTED"},
  {"key": "provider_capabilities", "_id": "x", "site_id": "s", "download": 1000000, "upload": 1000000},
  {"key": "radio_ai", "_id": "x", "site_id": "s", "setting_preference": "auto"},
  {"key": "rsyslogd", "_id": "x", "site_id": "s", "enabled": true, "ip": "10.0.0.1"},
  {"key": "snmp", "_id": "x", "site_id": "s", "enabled": false, "enabledV3": false},
  {"key": "ssl_inspection", "_id": "x", "site_id": "s", "state": "off"},
  {"key": "super_cloudaccess", "_id": "x", "site_id": "s"},
  {"key": "super_fabric_system_log", "_id": "x", "site_id": "s", "enabled": false},
  {"key": "super_fingerbank", "_id": "x", "site_id": "s", "fingerbank_key": "REDACTED"},
  {"key": "super_identity", "_id": "x", "site_id": "s", "hostname": "example"},
  {"key": "super_mail", "_id": "x", "site_id": "s", "provider": "cloud"},
  {"key": "super_mgmt", "_id": "x", "site_id": "s", "autobackup_enabled": true},
  {"key": "teleport", "_id": "x", "site_id": "s", "enabled": true, "subnet_cidr": "192.168.2.1/24"},
  {"key": "traffic_flow", "_id": "x", "site_id": "s", "enabled_allowed_traffic": true},
  {"key": "ugw", "_id": "x", "site_id": "s", "supports_vlan_group": false},
  {"key": "usg", "_id": "x", "site_id": "s", "upnp_enabled": true},
  {"key": "usg_geo", "_id": "x", "site_id": "s", "ip_filtering": {"enabled": false}}
]
```

- [ ] **Step 2: Write the regression test**

Append to `tests/test_coverage.py`:

```python
def test_real_udm_regression_section_gaps(schema, fixtures_dir):
    """Pin the section-gap set from the 2026-07-10 live-UDM audit.

    The Task 2 fixture schema covers mgmt/dpi/syslog/ips only, so the
    covered-in-production sections missing from IT are also expected gaps
    here (marked #schema-fixture below). When a provider PR adds a section,
    move its name out of EXPECTED — that reviewed edit IS the coverage
    change.
    """
    live = json.loads(
        (fixtures_dir / "coverage" / "settings_live.json").read_text())
    gaps, accepted = audit_settings(live, setting_schema_sections(schema))
    gap_sections = {f.identifier for f in gaps if f.kind == "section"}
    EXPECTED = {
        # real gaps on the audited UDM (provider PR backlog):
        "connectivity", "dashboard", "element_adopt", "ether_lighting",
        "global_nat", "global_network", "global_switch", "guest_access",
        "ipsec", "locale", "magic_site_to_site_vpn", "mdns", "netflow",
        "openvpn", "peer_to_peer", "provider_capabilities", "radio_ai",
        "snmp", "ssl_inspection", "teleport", "traffic_flow", "ugw",
        "usg_geo",
        # covered in production but absent from the trimmed schema fixture:
        "auto_speedtest", "country", "doh", "igmp_snooping", "lcm",
        "ntp", "network_optimization", "usg",
    }
    assert gap_sections == EXPECTED
    # super_* accepted (console-scope), super_cloudaccess empty -> absent.
    assert {f.identifier for f in accepted} == {
        "super_fabric_system_log", "super_fingerbank", "super_identity",
        "super_mail", "super_mgmt"}
    # mgmt is covered: its secret internal surfaces as a FIELD gap.
    assert any(f.identifier == "mgmt.x_mgmt_key" for f in gaps)
    # ips_suppression folds into ips — never a section gap.
    assert "ips_suppression" not in gap_sections
```

- [ ] **Step 3: Run, reconcile expectations**

Run: `uv run --extra dev pytest tests/test_coverage.py -q`
Expected: PASS. If the set differs, the DIFF is the finding — verify each unexpected/missing name against the audit rules (empty body? alias? classified?) before touching EXPECTED; do not blind-edit the set to green.

- [ ] **Step 4: Run the full suite, mypy, ruff**

Run: `uv run --extra dev pytest -q && uv run --extra dev mypy && uv run --extra dev ruff check src tests`
Expected: all PASS/clean

- [ ] **Step 5: Commit**

```bash
git add tests/fixtures/coverage/settings_live.json tests/test_coverage.py
git commit -m "tests: pin real-UDM coverage-gap regression set"
```

---

### Task 10: Docs and release prep

**Files:**
- Modify: `README.md` (the "Supported UniFi resources" section and the subcommand list)
- Modify: `CHANGELOG.md`
- Modify: `pyproject.toml` (version)

**Interfaces:** none — documentation of Tasks 1–9.

- [ ] **Step 1: Update README**

In `README.md`:
- In the subcommand descriptions, note that `enumerate` now requires a tofu-init'd `workdir` (it reads the provider schema for the coverage audit).
- Replace the paragraph starting "ubitofu detects and reports resources the provider cannot manage" with a short section:

```markdown
## Coverage audit — nothing is silently ignored

Every run audits the live controller against the provider's actual schema
(`tofu providers schema -json`): setting sections and their fields, probed
API collections, and provider resources missing from ubitofu's own manifest.
Findings land in two places:

- the console report (`Coverage gaps:` section), and
- `COVERAGE.md` in the workdir — byte-stable, committed alongside your HCL.

`COVERAGE.md` is the acceptance ledger: a new gap arrives as a git diff (and
rides whatever drift-PR automation you run), merging that diff acknowledges
it, and a gap disappears only when a provider release actually models the
config. There are no ignore lists.
```

- [ ] **Step 2: Update CHANGELOG and version**

`pyproject.toml`: `version = "0.4.0"`.

Prepend to `CHANGELOG.md` (match the existing entry format in the file):

```markdown
## 0.4.0 — 2026-07-10

- Schema-driven coverage audit: every live setting section/field, probed
  endpoint, and provider resource is checked against
  `tofu providers schema -json`; findings render byte-stably into
  `COVERAGE.md` (written by reconcile/generate) and the console report.
- No silent ignoring: acceptance is a git merge of the COVERAGE.md diff;
  the only code-level classification is `super_*` (console-scope).
- Manifest-lag check: provider resources with no MANIFEST mapping are
  reported (currently flags `unifi_ap_group`).
- BREAKING: `enumerate` now requires a tofu-init'd workdir (provider schema
  is mandatory — no degraded mode).
- Removed: `UNMAPPED_ENDPOINTS`, `_guest_network_gaps` (both folded into
  the audit; guest networks still reported, by `coverage.audit_guest_networks`).
```

- [ ] **Step 3: Full verification**

Run: `uv run --extra dev pytest -q && uv run --extra dev mypy && uv run --extra dev ruff check src tests`
Expected: all PASS/clean

- [ ] **Step 4: Commit**

```bash
git add README.md CHANGELOG.md pyproject.toml
git commit -m "release: v0.4.0 — schema-driven coverage audit"
```

---

## Post-merge follow-ups (separate repos — NOT part of this plan)

- `infra/ansible`: bump `.ubitofu-version` to the 0.4.0 tag; the next nightly reconcile writes the first `COVERAGE.md` into `infra/unifi/` and opens a chunky one-time drift PR (every currently-unmodeled field surfaces — expected, by design).
- `infra/ansible`: the `unifi_ap_group` MANIFEST entry and the guest-network discriminator change are separate, already-agreed ubitofu changes; the manifest-lag check and `audit_guest_networks` report both until they land.
- Provider fork: the PR backlog from the 2026-07-10 audit (see `docs/superpowers/specs/2026-07-10-coverage-audit-design.md` and the session notes) — each landed section PR shrinks the Task 9 EXPECTED set.
