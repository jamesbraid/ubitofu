# ubitofu product brief

## Purpose

UniFi administrators often create and refine networks through the controller UI
or mobile app. Those interfaces are practical operational tools, but their
changes normally exist outside the infrastructure repository. Traditional IaC
workflows treat every controller-side change as drift to overwrite.

ubitofu turns live UniFi configuration into maintainable OpenTofu HCL. It
supports the initial conversion of an existing network and the ongoing capture
of intentional UI or mobile-app changes as reviewable source changes.

## Product promise

Manage UniFi through both its native interfaces and OpenTofu HCL. ubitofu
converts controller changes into reviewable code and prevents code-driven
workflows from silently overwriting uncaptured controller changes.

## Core workflow

ubitofu observes three representations of the network:

- committed HCL records declared intent
- OpenTofu state records the last managed result
- the live controller records operational reality

It uses those observations to support four jobs:

1. **Generate:** bootstrap provider-compatible resources, imports, variables,
   and coverage information from an existing controller.
2. **Reconcile:** convert later UI or mobile-app changes into precise HCL edits
   while preserving compatible code changes.
3. **Review:** report conflicts, unsupported structures, secrets, and lifecycle
   changes before either authoring surface overwrites the other.
4. **Verify:** decide whether an exact OpenTofu plan is safe to hand to an
   external deployment system.

Controller changes are proposed intent rather than automatically authoritative.
HCL changes are declared intent, but they may not silently overwrite controller
changes that have not been captured. Compatible changes merge. Incompatible
changes to the same managed value stop with a precise conflict.

## Product boundary

ubitofu owns:

- UniFi discovery and response validation
- provider-schema-aware HCL generation
- comparison of controller, state, saved plans, and committed HCL
- semantic reconciliation and conflict classification
- byte-preserving HCL edits and transactional file updates
- plan safety decisions and UniFi health interpretation
- deterministic human reports, versioned JSON receipts, and stable exit codes

ubitofu does not own:

- writes to the UniFi controller
- `tofu apply`, import, or state mutation
- backend provisioning or state backup
- Git commits, branches, pushes, or pull requests
- CI scheduling, vendor-specific pipelines, or notifications
- deployment-specific secret and `TF_VAR_*` policy
- installation or upgrading of OpenTofu and providers

## Interface principles

- Commands perform one operator-visible operation and compose through files,
  stdout, stderr, and exit codes.
- Human output and versioned JSON receipts describe the same decisions.
- Behavior never depends on detecting Woodpecker, GitHub Actions, or another CI
  vendor.
- Missing prerequisites fail with an actionable instruction. ubitofu does not
  silently alter the surrounding toolchain.
- Sensitive values stay out of generated HCL, diagnostics, and receipts.
- CI integrations call the public CLI rather than importing internal Python
  modules.

## Scope test

A feature belongs in ubitofu only when it is necessary to generate, reconcile,
or safely verify UniFi OpenTofu configuration and can preserve the non-applying
boundary. Repository publication, deployment execution, backend recovery, and
notification features belong in adjacent tools even when moving them into
ubitofu would shorten one consumer's script.
