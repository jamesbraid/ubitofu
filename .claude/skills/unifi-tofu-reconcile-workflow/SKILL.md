---
name: unifi-tofu-reconcile-workflow
description: Use when adopting a new UniFi object into OpenTofu, reconciling UniFi UI config drift into HCL, or editing ubiquiti-community/unifi provider resources (unifi_device, unifi_wlan, unifi_network, unifi_port_profile). Triggers on ubitofu reconcile/generate, unifi_device import, or "add my new AP/network to tofu".
---

# UniFi-Tofu Reconcile Workflow

## Overview

**Never hand-author `ubiquiti-community/unifi` HCL from scratch.** The schema is finicky:
settable vs computed vs read-only attributes are non-obvious, `index`/`forward` fields have
ordering quirks, and shapes vary by object type. Hand-written blocks get rejected on first
plan or show perpetual drift. Let `ubitofu` emit the shape from live controller state, then
refine it.

## When to Use

- You adopted a new device, WLAN, network, or port profile in the UI and want tofu to manage it.
- You changed a managed object in the UI and want the edit reflected in HCL (drift).
- You need to write or extend `unifi_*` HCL and are unsure of an attribute's shape.

## Entry point 1 — new UI-adopted object

1. Configure the object in the UI until it behaves correctly.
2. Run `ubitofu reconcile`. It finds the object absent from state and writes a resource
   block plus matching `import` block to `reconciled_new.tf` — correct schema, live values.
3. Refine: rename the resource slug, add intent comments, confirm secret literals became
   `var.<name>` references.
4. Distribute: move the resource block to your `unifi-*.tf` of choice. Keep the `import`
   block in config until after the first apply.
5. PR, then `tofu plan` (must be clean), merge, `tofu apply`.

## Entry point 2 — UI edit to a managed object (drift)

Run `ubitofu reconcile`. It rewrites only the changed literal in place (see Safety model).
Review the diff, merge, apply. Nested or list-valued drift is flagged, not auto-edited.

## Entry point 3 — tofu-driven change

Edit HCL yourself. For any attribute whose shape is unclear, cross-check with
`ubitofu generate` or the provider schema rather than guessing. PR, plan, merge, apply.

## Safety model

- **Scalar-only:** only top-level scalars auto-edit; nested/list fields are flagged.
- **Anchor-checked:** a scalar changes only when the committed literal matches the expected
  old value; a mismatch flags the field instead of overwriting.
- **Stable-id matching:** objects match by MAC, composite key, or controller id — never by
  slug, so renaming a slug does not confuse reconcile.
- **Byte-preserving and idempotent:** running twice on a reconciled file yields no diff.
- **Secrets to vars:** PSKs, passwords, and keys become `var.<name>`, never plaintext.
  Reconcile also emits the `variable {}` block; set `TF_VAR_<name>` before apply.

## Quick reference — report sections

| Section | Meaning | Action |
|---|---|---|
| merged | Scalar updated in place in your `.tf` | Review diff, merge |
| appended | New object written to `reconciled_new.tf` | Refine, distribute, PR |
| complex / nested drift | Nested or list field differs; path given | Resolve manually |
| orphaned-state | In state, no matching resource block | DESTROYED next apply — restore block or `state rm` |
| diverged | deleted (gone on controller), pending (never applied), or inconsistent | Resolve per sub-state |
| secret-var warning | Secret-shaped value found; `variable {}` emitted | Declare it, set `TF_VAR_<name>` |

`reconciled_new.tf` is reconcile-owned scratch for new objects. `imports.tf` is your
hand-maintained adoption list; reconcile never touches it. `import` blocks go inert after
the first apply — delete them for tidiness whenever.

## Common mistakes

- Hand-writing a `unifi_device`/`unifi_wlan` block instead of running `ubitofu reconcile`.
- Deleting an `import` block before the first apply — tofu recreates a duplicate.
- Ignoring an `orphaned-state` line — the object gets destroyed on next apply.
