---
name: unifi-tofu-reconcile-workflow
description: Use when adopting or refining UniFi objects into OpenTofu with ubitofu, handling UI-edit drift, or authoring UniFi HCL — covers the tooling-first workflow, reconcile safety model, report sections, and secrets handling.
---

# UniFi-Tofu Reconcile Workflow

## Core principle: never hand-author UniFi HCL from scratch

The `ubiquiti-community/unifi` provider schema is finicky: the distinction between
settable, computed, and read-only attributes is non-obvious; `index`/`forward` fields
have ordering quirks; shapes vary by object type. Hand-writing risks a resource block
the provider rejects on first plan or one that perpetually shows drift. Let `ubitofu`
emit the correct shape from live controller state, then refine it.

---

## Three entry points (all tooling-first)

### 1. New object — adopted+configured in the UI

1. Configure the object in the UniFi controller UI until it behaves correctly.
2. Run `ubitofu reconcile`. It queries the live controller, finds the object absent
   from state, and emits a self-contained resource block + `import` block into
   `reconciled_new.tf` (correct schema, live values, no guessing).
3. **Refine:** rename the resource slug to something meaningful, add intent comments,
   replace any secret-shaped literals with `var.<name>` references.
4. **Distribute:** move the resource block to your `unifi-*.tf` file of choice.
   The `import` block travels with it (or stays in `reconciled_new.tf` — either is
   fine; it must remain in the config until after the first apply).
5. PR → `tofu plan` (must be clean) → merge → apply.
6. Optionally delete the `import` block; it is inert once applied and tofu will not
   re-create the object without it.

### 2. UI edit to a managed resource (drift)

1. After any UI change that you want reflected in HCL, run `ubitofu reconcile`.
2. reconcile matches the object by its **stable id** (MAC, composite key, or object
   id — not slug), detects the scalar drift, and surgically updates only the changed
   literal(s) in place. Comments and surrounding layout are preserved.
3. Review the diff, merge, apply.

> **Scope:** only top-level scalar drift is auto-edited. Nested/complex drift is
> flagged in the report (see below) for manual resolution.

### 3. Tofu-driven change

Edit HCL by hand or with an agent. For any new attribute whose shape is unclear,
cross-check with `ubitofu generate` or read the provider schema rather than guessing.
PR → `tofu plan` → merge → apply.

---

## `imports.tf` — what it is

A **transient adoption list**: `import` blocks that bind existing controller objects
into tofu state so tofu adopts them rather than creating duplicates. Once applied, the
blocks are inert. Delete them for tidiness at any time after the first apply.

`ubitofu reconcile` owns `reconciled_new.tf` for newly-discovered objects and never
touches your hand-maintained `imports.tf`.

---

## Reconcile safety model

- **Scalar-only auto-edit.** Only top-level scalars are written automatically. Nested
  or list structures are flagged, never silently mutated.
- **Anchor-safety.** The surgeon edits a scalar only when the committed literal in the
  file matches the expected old value. If it does not match, the field is flagged
  instead of overwritten.
- **Byte-preservation + idempotence.** Comments, whitespace, and formatting outside
  the edited token are untouched. Running reconcile twice on an already-reconciled
  file produces no diff.
- **Stable-id matching.** Objects are matched by MAC address, composite key, or
  controller object id — never by resource slug. Renaming a slug in HCL does not
  confuse reconcile, and new objects never reuse a managed slug.

---

## Reading the reconcile report

| Section | Meaning | Action |
|---|---|---|
| **merged** | Scalar(s) updated in place in your `.tf` files | Review diff, merge |
| **appended** | New object → resource+import written to `reconciled_new.tf` | Refine, distribute, PR |
| **complex / nested drift** | Nested or list-valued field differs; path given | Resolve manually |
| **orphaned-state** | Object is in tofu state but has no matching resource block | Will be **DESTROYED** on next apply — restore the block or explicitly remove from state |
| **diverged** | Plan for a committed resource diverged from expectations. Three sub-states: **deleted** — object removed on the controller but block+state still reflect it (remove the block, or re-create the object on the controller); **pending** — resource is in config but has never been applied (run `tofu apply`); **diverged** — controller state and config are inconsistent in a way that requires investigation | Resolve per sub-state label |
| **secret-var warning** | A secret-shaped value was found; a `variable {}` block was emitted | Declare the variable and set `TF_VAR_<name>` before applying |

---

## Secrets

Secret-shaped values (PSKs, passwords, API keys) become `var.<name>` references in
emitted HCL — never plaintext. For a newly-adopted object containing a secret,
reconcile also emits the `variable {}` declaration. Before applying:

```hcl
# emitted by reconcile — add to your variables file or keep in reconciled_new.tf
variable "my_network_psk" {
  type      = string
  sensitive = true
}
```

```bash
export TF_VAR_my_network_psk="<value>"
tofu apply
```

---

## CI/CD wiring

A downstream or internal skill can add CI/CD pipeline wiring (automated reconcile
runs, plan/apply gates, secret injection) on top of this generic workflow.
