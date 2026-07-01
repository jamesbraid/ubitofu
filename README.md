# unifi-tofu-import

Re-runnable CLI that enumerates a live UniFi/UDM controller and emits clean,
directly-appliable **OpenTofu** HCL for the `ubiquiti-community/unifi` provider.

Bulk-import an existing controller into OpenTofu, then re-run to reconcile code
with live config and surface drift. Plan-only and non-mutating — never runs
`tofu apply`, never writes to the controller.

**Status:** design phase. See [`docs/design.md`](docs/design.md).

## Non-goals
Resources the provider can't manage (NAT rules, DNS content-filtering, device
adoption, RF/firmware) are detected and reported, not managed.
