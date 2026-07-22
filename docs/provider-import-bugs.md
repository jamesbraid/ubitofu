# ubiquiti-community/unifi provider: import bugs blocking write scenarios

Status: OPEN — write scenarios in `tests/controllertest/` are parked on these.
Recorded 2026-07-22 for the provider-fork backlog (deliberately NOT filed
upstream). Full evidence: `.superpowers/sdd/task-9-report.md` on branch
`emdash/testing-6agni`, and reproducible any time with the parked S1 test.

## Reproduction context

- Controller: `ghcr.io/jamesbraid/unifi-network:10.4.57-seeded` (classic
  dialect, fresh site via `cmd/sitemgr add-site`, one seeded corporate
  network with VLAN + subnet).
- Provider: `ubiquiti-community/unifi` v0.55.0 from the public registry.
- Flow: `ubitofu generate` (HCL mirrors live REST values exactly) →
  `tofu apply` on the emitted `import {}` blocks + config.
- Deterministic: 3/3 runs, fresh site each time.
- Parked test: `tests/controllertest/test_scenarios_reconcile.py::test_s1_in_sync_reconcile_exits_zero`
  (skip-marked; remove the marker to reproduce).

## Bug 1 — import Read drops real network attributes → spurious update

The provider's `Read` during import-refresh returns null/unset for
attributes the controller genuinely has values for. Observed dropped on
`unifi_network`:

- `gateway_type` (live: `"default"`)
- `setting_preference` (live: `"auto"`)
- `dhcp_server.leasetime` (live: `"24h0m0s"` / `dhcpd_leasetime: 86400`)
- `ipv6_interface_type` (live: `"none"`)

Because config declares these values (correctly) and imported state lacks
them, tofu plans `~ update in-place (imported from ...)` on EVERY freshly
imported `unifi_network` — with zero real drift:

```
  # unifi_network.default will be updated in-place
  # (imported from "...")
  ~ resource "unifi_network" "default" {
      ~ dhcp_server        = { + leasetime = "24h0m0s" ... }
      + gateway_type       = "default"
      + setting_preference = "auto"
    }
```

### Consequence A: default network becomes un-adoptable when disabled

`rest/networkconf` is a full-object PUT. A site whose default network has
`enabled: false` (legitimate state) gets that value echoed in the forced
no-op update, and the controller rejects ANY default-network PUT carrying
`enabled: false` — even a non-change:

```
Error Updating network
  with unifi_network.default,
api.err.DisablingDefaultNetworkNotAllowed (400) for PUT
  .../rest/networkconf/<id>
payload: {"_id": "...", ..., "enabled": false, ..., "name": "Default", ...}
```

Fix directions: make import Read round-trip the attrs above so no spurious
update is planned; and/or never send `enabled` in the PUT for the default
network when it is not changing.

## Bug 2 — `domain_name` null → "" consistency error on ordinary networks

On the forced update of a freshly imported ordinary network, the PUT
response carries `domain_name: ""` where state/config had null. The
provider SDK's plan/apply consistency check kills the apply; the provider's
own error text labels it a provider bug:

```
Error: Provider produced inconsistent result after apply
When applying changes to unifi_network.s1_net, provider
"registry.opentofu.org/ubiquiti-community/unifi" produced an unexpected
new value: .domain_name: was null, but now cty.StringVal("").
```

Fix direction: normalize `""` ↔ null for `domain_name` in Read/Update
responses (plan-modifier or state normalization), as done for other
Optional+Computed string attrs.

## Impact on the controllertest suite

Every write scenario calls `adopt()` (generate → apply) and is parked until
a fixed provider build is available: S1–S5, S6a, S7, S8, S10. Unaffected
and implemented: smokes (S0), seeder/sandbox coverage, S6b (device deleted,
no apply), S9 (unreachable URL), UOS S11 (no apply).

Un-parking checklist: point the sandbox at the fixed provider (registry
release or `dev_overrides` against the local fork build), remove the skip
marker on S1, then implement S2–S10 from the plan
(`docs/superpowers/plans/2026-07-19-container-controller-testing.md`,
Tasks 10–13).
