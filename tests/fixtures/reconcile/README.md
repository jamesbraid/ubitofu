# Reconcile test fixtures

Sanitized fixtures shaped like real `tofu show -json` plan output and UniFi
controller device lists. Every identifying value is a placeholder.

## Scrubbing rules applied

- Device MACs are deterministic placeholders: `58:d6:1f:00:00:0a` (MAC A) and
  `58:d6:1f:00:00:0b` (MAC B). Both share the base name `U7 Pro Wall` to produce
  the slug-collision shape Task 3 exercises.
- Object IDs are 24-hex placeholders (e.g. `aabbccddeeff001122334455`).
- Passphrase, PSK, and key values are the literal `REDACTED`.
- Host names, domains, and public IPs are replaced with `example.test` and
  `10.0.0.0/8` placeholders.

## Fixture inventory

| File | Shape exercised |
|------|----------------|
| `committed.tf` | Single managed device (MAC A) — no WLAN block |
| `plan_two_same_name.json` | Two `unifi_device` entries sharing base name `u7_pro_wall`; MAC A is no-op, MAC B is create |
| `plan_orphan.json` | Managed device (no-op) + orphaned `unifi_port_forward.example_fwd` with `delete` action absent from `committed.tf` |
| `plan_new_secret.json` | Managed device (no-op) + new `unifi_wlan.example_net` create with `passphrase = "REDACTED"` |
| `controller_devices.json` | Two-element device list with same `name` and distinct MACs |

## Shapes

The device-collision shape follows `unifi_device` blocks keyed by MAC. The orphan
shape uses a `unifi_port_forward` present in state but absent from committed
config. The WLAN secret shape follows a `unifi_wlan` whose `passphrase` is a
write-only sensitive field.
