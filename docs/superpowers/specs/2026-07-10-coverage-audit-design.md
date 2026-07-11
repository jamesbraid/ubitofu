# Schema-driven provider-coverage audit

**Date:** 2026-07-10
**Status:** approved (design)

## Problem

ubitofu today reports coverage gaps through three ad-hoc mechanisms:
`UNMAPPED_ENDPOINTS` (three hardcoded endpoints), `_SKIP_LABELS` /
`_skip_reason` (per-object skips), and the one-off `_guest_network_gaps`.
Two classes of live config are silently ignored:

1. **Inside `get/setting`.** ubitofu imports the `unifi_setting` singleton by
   site but never inspects its sections. The audited UDM carries 42 live
   sections; the provider handles 13. The other 29 — including real config in
   `global_switch`, `global_network`, `mdns`, `teleport`,
   `magic_site_to_site_vpn`, `ether_lighting` — are invisible.
2. **Endpoints outside the manifest.** Collections such as
   `rest/scheduletask`, `rest/dpigroup`, `rest/wlangroup`,
   `v2 .../trafficrules`, `qos-rules`, `acl-rules` are never probed.

The `UNMAPPED_ENDPOINTS` list also goes stale: `apgroups` is still listed as
"no provider resource" although the jamesbraid/unifi fork has shipped
`unifi_ap_group`.

**Requirement (hard):** no silent ignoring. Every live controller item must
surface somewhere a human can see and diff.

## Core invariant

Every live item — endpoint object, setting section, setting field — lands in
exactly one bucket:

| Bucket | Meaning | Rendered as |
|---|---|---|
| **managed** | representable via MANIFEST + provider schema | (import targets, as today) |
| **gap** | live config the provider schema cannot express | Gaps section, warn |
| **classified** | structurally out of scope, with a written reason | Accepted section, always printed |
| **unknown** | matches nothing — new controller behaviour | Gaps section, prefixed `UNKNOWN` |

No code path drops an item without it appearing in the report. The three
legacy mechanisms fold into this model (`_skip_reason` rules remain as the
implementation of per-object gaps; their output joins the same report).

**Acceptance lives in git, not in code.** The committed `COVERAGE.md` is
the acknowledgment ledger: a new gap line arrives in a drift PR, and
merging that PR is the classification act — the PR discussion records the
reason. ubitofu carries no per-item ignore list. To actually silence a
line, close it at the source of truth: a provider PR that models the field
or section — settable if it is real config, **computed + sensitive** if it
is a controller internal (`x_mgmt_key`, `utm_token`, mesh PSKs). go-unifi's
generated structs already carry these fields, so such PRs are mechanical.
Steady state is an empty Gaps list; anything in it is either a work item
or visibly accepted-by-merge.

## Decisions (made during brainstorm)

- **Knowledge source: provider schema at runtime.** ubitofu reads
  `tofu providers schema -json` and diffs live payloads against the actual
  provider schema. Coverage self-updates when the provider pin bumps; no
  hand-maintained "covered sections" list to go stale.
- **Notification: baseline-file diff.** Reconcile rewrites a committed
  `COVERAGE.md` in the workdir. New/changed/closed gaps appear as a git diff
  and ride the existing drift machinery (branch → PR → Pushover). Known
  accepted gaps stay quiet. Report-only and always-notify were rejected
  (unnoticed gaps / alert fatigue).
- **Scope: field-level diffing only for `unifi_setting`.** Non-setting
  resources keep object-level checks; the HCL writer already knows their
  shapes, and field diffs there would be noise. Extension point noted below.

## Components

### 1. `coverage.py` (new)

`audit(ctl: Controller, schema: ProviderSchema) -> CoverageReport`

Pure function over three inputs: controller snapshot, provider schema,
manifest inventories. Produces typed findings; no I/O of its own.

**Endpoint check.** Probe every endpoint in `PROBE_ENDPOINTS`
(manifest.py, replaces `UNMAPPED_ENDPOINTS`). Endpoints already claimed by
`MANIFEST` specs are derived from the manifest, never repeated in the probe
list. Rules:

- Non-empty unmapped collection → gap (count + endpoint + label).
- Objects with `attr_no_delete` or `attr_hidden_id` → classified
  "built-in default" via one generic rule (covers `dpigroup`, `wlangroup`
  defaults without per-resource hacks).
- Probe returns 400/404 → classified "endpoint not present on this
  controller version". Recorded, not dropped.

Initial probe universe beyond the current three: `rest/scheduletask`,
`rest/dpigroup`, `rest/dpiapp`, `rest/wlangroup`, `rest/hotspotop`,
`rest/hotspotpackage`, `rest/hotspot2conf`, `rest/channelplan`,
`v2 .../trafficrules`, `.../qos-rules`, `.../acl-rules`, `.../wan-slas`,
`.../device-tags`.

**Setting-section check.** Live `get/setting` keys vs the top-level
attributes of the `unifi_setting` resource schema. One alias map:
`syslog ↔ rsyslogd`. Rules:

- Live section, no schema attribute, not classified → gap
  ("setting section `mdns` has live config; provider lacks it").
- Live section in neither schema nor `CLASSIFIED_SECTIONS` → unknown gap.
- Schema attribute with no live section → nothing (not a gap).

**Setting-field check.** For each section the schema covers, diff the live
JSON keys against the schema's nested attributes. Names are compared after
normalization: lowercase, underscores stripped (`fingerprintingEnabled` ==
`fingerprinting_enabled`). Every unmatched live field → field gap, no
exceptions and no ignore list; controller internals are silenced by
provider PRs that model them as computed + sensitive attributes (see
"Acceptance lives in git"). This is also what catches a new
controller-version field inside an already-covered section.

**Manifest-lag check.** Resource types present in the provider schema but
absent from `MANIFEST` render as a gap: "provider supports X; ubitofu
manifest does not map it". This is the inverse direction — it catches
ubitofu falling behind its own provider (exactly the current stale
`apgroups` situation).

### 2. `tofu_runner.provider_schema(workdir)`

Runs `tofu providers schema -json`, parses the `unifi_setting` attribute
tree plus the resource-type list. Requires an init'd workdir. On failure:
abort with a clear "run tofu init in <workdir>" error. **No degraded
silent mode** — a missing schema would reintroduce silent ignoring.

### 3. Manifest inventories (manifest.py)

- `PROBE_ENDPOINTS: dict[endpoint, label]` — the probe universe. Hand-kept;
  changes rarely (new controller versions).
- `CLASSIFIED_SECTIONS: dict[section-glob, reason]` — deliberately tiny.
  Only entries that are *structurally* out of scope and can never be closed
  by a provider PR belong here. Seed: `super_*` (console-scope; a
  site-scoped `unifi_setting` should never model it). Everything else from
  the audit — `connectivity`, `element_adopt`, `peer_to_peer`, `openvpn`,
  `provider_capabilities`, `ugw` — is NOT classified: those lines sit in
  the baseline as gaps until a provider PR models them (settable or
  computed + sensitive) or a merge visibly accepts them.
- There is no `CLASSIFIED_FIELDS` and no per-field ignore list of any kind
  (rejected: a second inventory duplicating what git-merge acceptance and
  provider schema PRs already provide; prefix heuristics like `x_*` are
  wrong because some `x_` fields are real config).
- Per-object classification is not needed: the generic
  `attr_no_delete`/`attr_hidden_id` rule plus `_skip_reason` cover it.

Every entry carries a human-readable reason string; the classified list is
itself part of the rendered report.

### 4. `COVERAGE.md` baseline

Written into the workdir (`infra/unifi/COVERAGE.md`) by **reconcile** on
every run; **enumerate** prints identical content to stdout. Properties:

- Deterministic ordering (sorted), no timestamps, no counts that flap —
  byte-stable across runs against an unchanged controller, so it never
  produces a spurious drift PR.
- Two sections: **Gaps** (warn-worthy, includes UNKNOWN items first) and
  **Accepted** (classified items with reasons).
- Each gap line carries enough to act on: kind (endpoint / section /
  field / object), the identifier, object count or field list, and the
  label ("no provider resource", "provider lacks attribute", …).

CI integration needs **zero pipeline changes**: a changed `COVERAGE.md`
makes `git status --porcelain infra/unifi` non-empty in
`ci/unifi-reconcile.sh`, so the existing branch/PR/Pushover path fires. A
provider PR landing (gap closes) also produces a diff — closure is visible
too.

### 5. Reporter

The enumerate/reconcile report gains a `coverage` section rendering the
same findings (gaps first, then a one-line count of accepted items with a
pointer to `COVERAGE.md`). Existing gap lines (`unifi_bgp skipped`,
`_skip_reason` counts) move under it — one home for all coverage output.

## Error handling

- Schema fetch failure → hard error, actionable message. Never proceed
  without schema.
- Endpoint probe HTTP error → classified finding (see above), run continues.
- Unknown setting section / unmatched field → gap, run continues.
- Malformed `COVERAGE.md` on disk is irrelevant — it is always fully
  rewritten, never parsed.

## Testing

- **Golden tests:** fixture controller payloads + fixture
  `providers schema -json` → expected `COVERAGE.md` bytes. Cases: unmapped
  populated endpoint; unknown section; covered section with unknown field;
  camelCase field normalization; syslog/rsyslogd alias; default-object
  filtering; 404 endpoint.
- **Invariant test:** for synthetic snapshots, every generated live item
  appears in exactly one bucket — the no-silent-ignoring guarantee enforced
  structurally, not by review.
- **Byte-stability:** two runs over the same fixtures produce identical
  bytes.
- **Real-controller regression fixture:** sanitized snapshot of the audited
  UDM pinning the current gap set (global_switch, global_network,
  global_nat, mdns, teleport, magic_site_to_site_vpn, traffic_flow,
  ether_lighting, locale, ipsec, usg_geo, snmp, netflow, ssl_inspection,
  guest_access, radio_ai, dashboard sections; NAT + content-filtering +
  APP-policy objects; guest network).

## Out of scope (v1)

- Field-level diffing for non-setting resources (extension point:
  `coverage.py` takes the full schema; adding per-resource field checks is
  additive).
- Enum-value gaps derived from schema (validators are not in the schema
  dump; `_skip_reason` hand rules remain the mechanism).
- Any ignore-list UI/config outside `manifest.py` — classifications are
  code, reviewed via PR like everything else in ubitofu.

## Migration notes

- `UNMAPPED_ENDPOINTS` deleted; `_guest_network_gaps` deleted once the
  guest-network discriminator change lands (tracked separately); if this
  ships first, the guest check moves into `coverage.py` unchanged.
- `unifi_ap_group` manifest entry (separate, already-agreed change) removes
  the stale apgroups gap; until then the manifest-lag check correctly
  reports it, because the schema contains the resource while the manifest
  does not map its endpoint.
