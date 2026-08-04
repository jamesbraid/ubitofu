---
name: unifi-tofu-reconcile-workflow
description: Use when generating ubiquiti-community/unifi HCL from a live controller, capturing UniFi UI or mobile changes, reconciling concurrent HCL intent, or reviewing a ubitofu dry-run, blocking outcome, or JSON receipt.
---

# UniFi and OpenTofu reconciliation

## Boundary

Use ubitofu to turn live controller configuration into provider-shaped HCL and
to reconcile later UI/mobile changes with HCL intent. Do not make controller
changes, apply an OpenTofu plan, mutate state, edit Ansible, commit to Git, or
operate CI from this skill. Hand the reviewed HCL diff and typed outcome back to
the caller's deployment workflow.

Do not hand-author a new `ubiquiti-community/unifi` resource when the controller
can provide the shape. Configure or adopt the object in the UniFi UI first, then
let `generate` or `reconcile` derive the provider-facing representation.

## Initial generation

1. Confirm the intended objects work in the UniFi UI or mobile app.
2. Confirm the OpenTofu root is initialized and no other OpenTofu process, Git
   automation, or file watcher is using it.
3. Run `ubitofu generate --config CONFIG`.
4. Review every generated resource, import, variable declaration, and
   `COVERAGE.md` finding.
5. Stop on exit 3. Do not replace operator-owned content or bypass a coverage,
   source-ownership, or metadata finding.

Generation is the bootstrap path. It may publish a short-lived reserved import
scaffold while holding the ubitofu lock. Reconciliation and dry-run never use
that scaffold.

## Reconcile UI/mobile and HCL changes

Always preview before allowing a wet reconcile:

```console
ubitofu reconcile --dry-run --config CONFIG \
  --format json --output DRY_RUN_RECEIPT
```

Review the typed decisions, changed paths, and candidate digests. Dry-run does
not write HCL or recover old mutation residue. Exit 3 means the preview is valid
but blocked, so resolve the named conflict or unsafe source fact and preview
again.

When the preview is allowed, run:

```console
ubitofu reconcile --config CONFIG \
  --format json --output RECONCILE_RECEIPT
```

Review the ordinary HCL diff and the receipt together. Wet and dry mode produce
the same plan, candidates, decisions, paths, and candidate digests only when
their collected inputs are equivalent. A controller edit between the two runs
can legitimately change the result.

## Interpret decisions

ubitofu compares the last managed value, evaluated HCL intent, and the current
controller value.

| Change | Result |
| --- | --- |
| UI/mobile only | capture the controller value in HCL |
| HCL only | preserve the declared value |
| both changed to the same value | converged, with no conflict |
| both changed the same comparable field differently | block the whole reconcile |
| independent fields changed | merge into one candidate set |

Collections merge only where the manifest defines a stable element identity.
Other complex collections are atomic. Never choose a winner for a typed
same-field conflict. Ask the operator to make the controller and HCL intent
unambiguous, then rerun dry-run.

`source_ownership_ambiguous` means a non-literal expression could not be mapped
to one safe source edit. JSON HCL is indexed but read-only, so
`json_source_read_only` requires the operator to move or resolve that intent.
Unsupported ACLs, extended attributes, file flags, symlinks, ownership, and
stale identities fail closed. Do not remove those checks to force a write.

## Secrets

Controller secrets never become HCL literals, output fields, receipt values, or
digests. An explicit supported secret rule may render a `var.<name>` reference.
Other provider-declared sensitive and write-only values stay suppressed.

- HCL-only secret rotation is allowed.
- Detectable controller-only secret change blocks because it cannot be captured.
- Detectable different changes on both sides block as a secret conflict.
- Unavailable or write-only observations remain noncomparable.

`secret_freshness_unverified` from `check --plan` is an advisory warning, not
proof that a post-plan UI secret edit is safe to overwrite. Report the warning
to the operator and leave the apply decision to the external workflow.

## Saved-plan and health review

Use only the caller-supplied plan:

```console
ubitofu check --plan PLAN --config CONFIG \
  --format json --output CHECK_RECEIPT
```

Confirm the receipt names the saved-plan digest and inspect every warning or
blocking decision. ubitofu does not create, apply, back up, or remove the plan.

Use `ubitofu health snapshot` for the pre-change baseline and
`ubitofu health compare --before RECEIPT` for the later observation. A new
degradation, newly unknown state, missing subsystem, or missing baseline blocks.
An unchanged unknown baseline is advisory.

## Exit codes

| Code | Meaning | Response |
| ---: | --- | --- |
| 0 | success, allowed plan, or warning | inspect warnings and any source diff |
| 1 | operational failure | fix connectivity, OpenTofu, filesystem, or output failure |
| 2 | usage or configuration error | correct the command or config |
| 3 | valid blocking outcome | resolve the typed finding and do not force a write |

Do not parse human prose to recover policy. Use JSON receipts and their typed
items. Human and JSON formats come from the same outcome, and neither contains
HCL values or controller secrets.
