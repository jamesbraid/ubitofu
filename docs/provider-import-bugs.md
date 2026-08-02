# unifi provider: import bugs blocking write scenarios

Status: OPEN — write scenarios in `tests/controllertest/` are parked on these.
First recorded 2026-07-22 against `ubiquiti-community/unifi` v0.55.0; retested
2026-08-02 against `jamesbraid/unifi` 0.101.1, the version the sandbox now pins.
Both bugs survive. Kept for the provider-fork backlog (deliberately NOT filed
upstream). Reproducible any time with the parked S1 test below.

## Reproduction context

- Controller: `ghcr.io/jamesbraid/unifi-network:10.4.57-seeded` (classic
  dialect, fresh site via `cmd/sitemgr add-site`, one seeded corporate
  network with VLAN + subnet).
- Provider: `registry.terraform.io/jamesbraid/unifi` 0.101.1, pinned in
  `tests/controllertest/pins.py`. Previously v0.55.0 from the public registry
  — which the sandbox never actually pinned, so "v0.55.0" in the first
  recording was whatever the registry served that day.
- Flow: `ubitofu generate` (HCL mirrors live REST values exactly) →
  `tofu apply` on the emitted `import {}` blocks + config.
- Deterministic: 3/3 runs on v0.55.0, 2/2 on 0.101.1, fresh site each time.
- Parked test: `tests/controllertest/test_scenarios_reconcile.py::test_s1_in_sync_reconcile_exits_zero`
  (skip-marked; remove the marker to reproduce).

## Bug 1 — import Read drops real network attributes → spurious update

The provider's `Read` during import-refresh returns null/unset for
attributes the controller genuinely has values for, so tofu plans
`~ update in-place (imported from ...)` on a freshly imported
`unifi_network` with zero real drift.

What 0.101.1 still drops, straight off the plan (`+` = absent in imported
state, present in config):

| attribute | default network | ordinary network |
|---|---|---|
| `gateway_type` (live `"default"`) | dropped | dropped |
| `dhcp_server.leasetime` (live `"24h0m0s"`) | dropped | n/a (no DHCP server) |
| `setting_preference` | dropped when `"auto"` | round-trips when `"manual"` |
| `ipv6_interface_type` (live `"none"`) | round-trips | dropped |

Narrower than v0.55.0, where all four dropped on both. The `"manual"` case
round-tripping while `"auto"` does not points at the encoder/decoder pair
that 0.101.x reworked, not at a generic null-handling bug.

```
  # unifi_network.default will be updated in-place
  # (imported from "6a6ee93a1dbc10d5d5fec7f7")
  ~ resource "unifi_network" "default" {
      ~ dhcp_server        = { + leasetime = "24h0m0s" ... }
        enabled            = false
      + gateway_type       = "default"
      + setting_preference = "auto"
    }
```

### Consequence A: default network becomes un-adoptable when disabled

`rest/networkconf` is a full-object PUT. A site whose default network has
`enabled: false` (legitimate state) gets that value echoed in the forced
no-op update, and the controller rejects ANY default-network PUT carrying
`enabled: false` — even a non-change. Unchanged on 0.101.1:

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
own error text labels it a provider bug.

Still present on 0.101.1. Bug 1 normally masks it — the default network's
rejected PUT fails the apply first — so it was reproduced in isolation with
`tofu apply -target=unifi_network.s1_net`:

```
Error: Provider produced inconsistent result after apply
When applying changes to unifi_network.s1_net, provider
"provider[\"registry.terraform.io/jamesbraid/unifi\"]" produced an unexpected
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

Un-parking checklist: bump `PROVIDER_VERSION` in
`tests/controllertest/pins.py` to the fixed build, remove the skip marker on
S1, then implement S2–S10 from the plan
(`docs/superpowers/plans/2026-07-19-container-controller-testing.md`,
Tasks 10–13).
