# Reconcile test fixtures

Real-shape fixtures derived from a live UniFi UDM controller (2026-07) and
`tofu show -json` plan output. All homelab-identifying material has been
scrubbed.

## Scrubbing rules applied

- Real device MACs replaced with deterministic fakes: `58:d6:1f:00:00:0a`
  (MAC A) and `58:d6:1f:00:00:0b` (MAC B). Both share the base name
  `U7 Pro Wall` to produce the slug-collision shape Task 3 exercises.
- Hex object IDs replaced with 24-hex placeholders (e.g. `aabbccddeeff001122334455`).
- All passphrase/PSK/WireGuard-key values replaced with the literal `REDACTED`.
- Public IPs and hostnames (`example`, `example`, `example`, `example`) replaced
  with `example.test` or `10.0.0.0/8` placeholders.

## Fixture inventory

| File | Shape exercised |
|------|----------------|
| `committed.tf` | Single managed device (MAC A) — no WLAN block |
| `plan_two_same_name.json` | Two `unifi_device` entries sharing base name `u7_pro_wall`; MAC A is no-op, MAC B is create |
| `plan_orphan.json` | Managed device (no-op) + orphaned `unifi_port_forward.traefik_preview` with `delete` action absent from `committed.tf` |
| `plan_new_secret.json` | Managed device (no-op) + new `unifi_wlan.example_net` create with `passphrase = "REDACTED"` |
| `controller_devices.json` | Two-element device list with same `name` and distinct MACs |

## Provenance

Shapes derived from real `generated.tf` / `imports.tf` blocks produced by
`tofu generate-config-out` against a live UDM controller. The U7 Pro Wall
collision shape is based on real `unifi_device` blocks keyed by MAC. The orphan
shape mirrors the real `unifi_port_forward.traefik_preview` resource that was
later removed from HCL. The WLAN secret shape mirrors real `unifi_wlan` blocks
where `passphrase` is a write-only sensitive field.
