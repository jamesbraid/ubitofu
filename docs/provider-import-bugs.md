# unifi provider: import bugs blocking write scenarios

Status: OPEN. These bugs and incomplete supported controller coverage park the
write scenarios in `tests/controllertest/`.
First recorded 2026-07-22 against `ubiquiti-community/unifi` v0.55.0, and
retested 2026-08-02 against `jamesbraid/unifi` 0.101.1, the version the sandbox
now pins. Both survive. Kept for the provider-fork backlog, deliberately NOT
filed upstream. The parked in-sync reconcile test below reproduces them on
demand.

## Reproduction context

- Controller: `ghcr.io/jamesbraid/unifi-network:10.4.57-seeded` (classic
  dialect, fresh site via `cmd/sitemgr add-site`, one seeded corporate
  network with VLAN + subnet).
- Provider: `registry.terraform.io/jamesbraid/unifi` 0.101.1, pinned in
  `tests/controllertest/pins.py`. Previously v0.55.0 from the public registry.
  The sandbox pinned nothing then, so that first recording names whatever the
  registry served that day.
- Flow after the fixture passes ubitofu's fail-closed coverage gate:
  `ubitofu generate` (HCL mirrors live REST values exactly) →
  `tofu apply` on the emitted `import {}` blocks + config.
- Deterministic: 3/3 runs on v0.55.0, 2/2 on 0.101.1, fresh site each time.
- Parked test: `tests/controllertest/test_scenarios_reconcile.py::test_in_sync_reconcile_exits_zero`
  (skip-marked; remove the marker to reproduce).

## Bug 1 — import Read drops real network attributes → spurious update

During an import-refresh the provider's `Read` returns null for attributes the
controller does hold values for. Tofu therefore plans
`~ update in-place (imported from ...)` on a freshly imported `unifi_network`
that has no drift at all.

These are the attributes 0.101.1 still drops, taken from the plan (`+` marks a
value absent from imported state and present in config):

| attribute | default network | ordinary network |
|---|---|---|
| `gateway_type` (live `"default"`) | dropped | dropped |
| `dhcp_server.leasetime` (live `"24h0m0s"`) | dropped | n/a (no DHCP server) |
| `setting_preference` | dropped when `"auto"` | round-trips when `"manual"` |
| `ipv6_interface_type` (live `"none"`) | round-trips | dropped |

v0.55.0 dropped all four on both networks, so this is narrower. That
`"manual"` round-trips while `"auto"` does not points at the encoder and
decoder pair 0.101.x reworked, rather than at a general null-handling bug.

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

### Consequence: a disabled default network cannot be adopted

`rest/networkconf` is a full-object PUT. When a site's default network is
legitimately disabled, the forced no-op update echoes `enabled: false` back,
and the controller rejects every default-network PUT that carries it, even one
that changes nothing. 0.101.1 behaves the same:

```
Error Updating network
  with unifi_network.default,
api.err.DisablingDefaultNetworkNotAllowed (400) for PUT
  .../rest/networkconf/<id>
payload: {"_id": "...", ..., "enabled": false, ..., "name": "Default", ...}
```

Two ways to fix it. Make import `Read` round-trip the attributes above, so
nothing plans a spurious update. Or leave `enabled` out of the PUT for a
default network that is not changing. Either would do; both would be better.

## Bug 2 — `domain_name` null → "" consistency error on ordinary networks

On the forced update of a freshly imported ordinary network, the PUT response
carries `domain_name: ""` where state and config held null. The plugin
framework's consistency check then kills the apply, and the provider's own
error text calls it a provider bug.

0.101.1 still does this. Bug 1 hides it, because the default network's rejected
PUT fails the apply first. Target the ordinary network on its own to see it:

```
tofu apply -target=unifi_network.in_sync_net
```

```
Error: Provider produced inconsistent result after apply
When applying changes to unifi_network.in_sync_net, provider
"provider[\"registry.terraform.io/jamesbraid/unifi\"]" produced an unexpected
new value: .domain_name: was null, but now cty.StringVal("").
```

To fix: treat `""` and null as the same value for `domain_name` in the Read and
Update responses, with a plan modifier or state normalization, as the other
Optional+Computed string attributes already do.

## Impact on the controllertest suite

Every scenario that writes calls `adopt()` (generate → apply), and adoption is
what fails, so all of them wait for a fixed provider. That covers the whole
write half of the suite: reconciling an in-sync site, capturing drift,
adopting a new object, staging a deleted one, and the rest.

The scenarios that never apply are unaffected and already run: the version
smokes, seeder and sandbox coverage, the deleted-device case (it plans and
classifies, it does not apply), and the unreachable-controller error path.
The seeded UniFi OS and herder cases also reach their provider-shaped generation
snapshots, then prove that known coverage gaps suppress every candidate.

To un-park: make the fixture site coverage-complete, bump `PROVIDER_VERSION` in
`tests/controllertest/pins.py` to the fixed build, remove the skip marker on the
in-sync reconcile test, then write the remaining write scenarios from the plan
(`docs/superpowers/plans/2026-07-19-container-controller-testing.md`).
