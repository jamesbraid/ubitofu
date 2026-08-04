# Ansible Consumer Cutover Implementation Plan

Status: superseded by `2026-08-03-ubitofu-0.10-single-cutover.md`. Retained as
review history. Do not execute this phase independently.

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> superpowers:subagent-driven-development (recommended) or
> superpowers:executing-plans to implement this plan task-by-task. Steps use
> checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace Ansible's UniFi-specific plan and health parsing with released
ubitofu commands while reducing shell maintenance and preserving repository-
specific deployment and publication policy.

**Architecture:** This phase runs in the private Ansible repository after phase
5 has shipped a public package. ubitofu owns controller/schema interpretation,
semantic plan safety, health normalization, receipts, and stable exits. Ansible
retains OpenTofu initialization, deployment variables, state backup, exact-plan
apply, Git publication, Woodpecker routing, PR creation, and notifications. A
temporary dual-run lane proves parity before each old parser is deleted.

**Tech Stack:** Released ubitofu wheel, OpenTofu CLI, Bash wiring, Python 3
stdlib contract tests, Woodpecker, existing deployment image, existing Git/PR
and notification tooling.

## Repository and Safety Constraints

- Execute this plan in `~/ansible`, not the ubitofu worktree. Read and obey that
  repository's `AGENTS.md` before editing.
- Do not copy private controller names, addresses, credentials, secret-store
  references, forge names, or notification destinations into ubitofu source,
  fixtures, documentation, commits, releases, or PR metadata.
- Do not run stateful reconcile/apply commands locally. Use the established
  Woodpecker workflow and inspect it with `woodpecker-cli`.
- Preserve the current pre-apply state-backup gate. A missing or failed backup
  still blocks apply.
- Preserve the current post-apply health behavior: baseline problems are noise;
  newly degraded subsystems make the pipeline red after apply.
- Preserve UI-only device adoption. A planned managed-device create remains a
  hard block.
- The exact file checked by `ubitofu check` must be the file supplied to
  `tofu apply`. Recompute its SHA-256 immediately before apply and compare it to
  the receipt.
- Do not move deployment `TF_VAR_*`, backend credentials, Git credentials,
  notification credentials, or 1Password policy into ubitofu.
- Do not delete an old script or decision branch until its scenario matrix has
  dual-run evidence and the replacement has a rollback commit.
- Commit locally in kernel style. Push/open a PR only when the user separately
  authorizes it.

## Target Boundary

| Concern | ubitofu | Ansible/Woodpecker |
| --- | --- | --- |
| controller and provider interpretation | owns | consumes outcome |
| semantic live/HCL/state merge | owns | publishes resulting Git diff |
| exact saved-plan safety | owns decision and digest | creates, hash-verifies, applies same plan |
| health normalization/comparison | owns | decides pipeline alert routing |
| HCL writes | owns transactionally | reviews and publishes diff |
| `tofu init` and backend access | never | owns |
| state backup and `tofu apply` | never | owns |
| deployment variables/secrets | never | owns |
| scheduling, path filters, PRs, notifications | never | owns |

---

### Task 1: Bake and pin the released public package

This is an image-only prerequisite commit. Land it and verify the rebuilt image
before changing any consumer step; otherwise a same-pipeline mutable-tag race
could run new shell against the old image.

**Files in `~/ansible`:**

- Modify: `.ubitofu-version`
- Modify: `ci/infra-deploy/Dockerfile`
- Modify: `.woodpecker/ci.yml`
- Create: `ci/tests/test_ubitofu_image_contract.py`

- [ ] Pin `.ubitofu-version` to the exact released phase-5 tag. Add a stdlib
  unittest that strips the leading `v`, finds the Docker build argument, and
  requires the runtime image smoke command to report the same package version.

- [ ] Change the build context from `ci/infra-deploy` to the repository root so
  the build step can read `.ubitofu-version`. Update Dockerfile `COPY` paths
  accordingly and keep the existing kaniko build/push mechanism.

- [ ] Add `ARG UBITOFU_VERSION` and install
  `ubitofu==${UBITOFU_VERSION}` from the public package index in the existing
  pinned Python tool layer. Woodpecker derives the build argument by removing
  exactly one leading `v` from `.ubitofu-version`; reject any other version-file
  shape in the contract test.

- [ ] Add `.ubitofu-version`, the Dockerfile, and the build-step definition to
  the build image's path filter. Do not add consumer changes to this commit.

- [ ] Run read-only local checks:

  ```bash
  python3 -m unittest discover -s ci/tests -p 'test_ubitofu_*.py' -v
  git diff --check
  ```

- [ ] Commit:

  ```bash
  git add .ubitofu-version ci/infra-deploy/Dockerfile .woodpecker/ci.yml \
    ci/tests/test_ubitofu_image_contract.py
  git commit -m "ci: bake the released ubitofu command surface"
  ```

- [ ] After authorized publication, use `woodpecker-cli` to verify the image
  build step passed. Start a manual smoke step that runs `ubitofu --version`,
  imports `tree_sitter_hcl`, and confirms the exact pin. Do not begin Task 2
  against an unverified mutable image tag.

---

### Task 2: Add private config and consumer contract tests

**Files in `~/ansible`:**

- Create: `infra/unifi/ubitofu.toml`
- Create: `ci/tests/test_unifi_ubitofu_contract.py`
- Modify: `.gitignore`
- Modify: `.woodpecker/ci.yml`

- [ ] Create a committed config by moving the current non-secret controller
  endpoint and site literals out of the temporary heredoc unchanged. Set
  `api_key_source = "env"`, `api_key_ref = "TF_VAR_unifi_api_key"`,
  `workdir = "."`, and the explicit current TLS migration choice. Commands must
  run with `infra/unifi` as cwd so `workdir = "."` is unambiguous.

- [ ] Add stdlib contract tests that use temporary fake `ubitofu`, `tofu`,
  `aws`, and controller fixtures rather than real infrastructure. Cover command
  arguments, saved-plan path reuse, receipt parsing, digest mismatch, outcome
  exits 0/10/11/12/13/1/2, health degradation, pre-health unavailable, backup
  failure, and cleanup.

- [ ] Add ignore rules for temporary saved plans, receipts, and health snapshots
  used by CI. Tests must also assert these artifacts do not appear in a Git diff.

- [ ] Add one lightweight `unifi-ubitofu-contract` Woodpecker step that runs:

  ```bash
  python3 -m unittest discover -s ci/tests -p 'test_unifi_ubitofu_contract.py' -v
  ```

  It needs no credentials or controller access and runs when any UniFi workflow,
  script, config, or version input changes.

- [ ] Run locally:

  ```bash
  python3 -m unittest discover -s ci/tests -p 'test_*ubitofu*.py' -v
  git diff --check
  ```

- [ ] Commit:

  ```bash
  git add infra/unifi/ubitofu.toml ci/tests/test_unifi_ubitofu_contract.py \
    .gitignore .woodpecker/ci.yml
  git commit -m "unifi: codify the ubitofu consumer contract"
  ```

---

### Task 3: Dual-run old and new safety decisions

**Files in `~/ansible`:**

- Create: `ci/unifi-ubitofu-dual-run.sh`
- Modify: `ci/tests/test_unifi_ubitofu_contract.py`
- Modify: `.woodpecker/ci.yml`
- Modify: `.woodpecker/unifi-reconcile.yml`

**Parity matrix:**

| Scenario | Legacy evidence | New evidence | Required agreement |
| --- | --- | --- | --- |
| no-op | `reconcile --check` | `check --plan` | allow/no drift |
| HCL-only change | old gate | saved-plan check | allow and preserve HCL intent |
| controller-only change | old gate | saved-plan check | block as uncaptured drift |
| disjoint concurrent changes | old classification | semantic plan check | compatible merge/allow |
| same-field concurrent change | old classification | semantic plan check | conflict/block |
| managed-device create | old exit 13 | plan decision | hard block |
| high-risk network/firewall/VPN change | shell parser | plan warning | same affected address set |
| unchanged baseline health error | old health script | `health compare` | no degradation |
| new health degradation | old health script | `health compare` | alert/fail |

- [ ] Implement the temporary script as read-only orchestration. It creates one
  saved plan, runs legacy `reconcile --check`, runs `ubitofu check` against that
  plan, captures both outcomes, and compares their normalized decision class.
  It never applies and always removes its plan/receipt on exit.

- [ ] Add fake-command contract tests for all matrix rows. The ubitofu phase-5
  fixtures provide semantic cases; the Ansible tests prove exit/receipt wiring.
  Do not mutate the live controller merely to manufacture parity evidence.

- [ ] Add a credentialed manual-only Woodpecker dual-run step. Run it against
  naturally available no-op or drift states and archive only redacted receipts
  and decision classes. A live run is additional evidence, not a substitute for
  the complete synthetic matrix.

- [ ] Run:

  ```bash
  bash -n ci/unifi-ubitofu-dual-run.sh
  python3 -m unittest discover -s ci/tests -p 'test_unifi_ubitofu_contract.py' -v
  git diff --check
  ```

- [ ] Commit:

  ```bash
  git add ci/unifi-ubitofu-dual-run.sh \
    ci/tests/test_unifi_ubitofu_contract.py .woodpecker/ci.yml \
    .woodpecker/unifi-reconcile.yml
  git commit -m "unifi: dual-run legacy and public safety decisions"
  ```

- [ ] Run the manual lane through Woodpecker and review the receipts. Stop if
  any disagreement is not explained by the deliberate stronger saved-plan
  contract. Fix ubitofu rather than encoding a semantic workaround in shell.

---

### Task 4: Check and apply one exact saved plan

**Files in `~/ansible`:**

- Modify: `ci/tofu.sh`
- Delete after green dual-run: `ci/unifi-gate.sh`
- Modify: `.woodpecker/ci.yml`
- Modify: `ci/tests/test_unifi_ubitofu_contract.py`

- [ ] Extend fake-command tests first. For `ci/tofu.sh unifi apply`, require this
  order and exact plan path identity:

  1. `tofu init`.
  2. `tofu plan -detailed-exitcode -out=<private-plan>`.
  3. `ubitofu check --config ubitofu.toml --plan <same-private-plan>
     --format json`, captured with `tee` into a private receipt.
  4. Optional pre-apply `ubitofu health snapshot`.
  5. mandatory external state backup.
  6. recompute plan SHA-256 and compare it with the receipt.
  7. `tofu apply -auto-approve <same-private-plan>`.
  8. post-apply `ubitofu health compare` when a baseline exists.
  9. trap cleanup of plan, receipt, and health snapshot.

- [ ] Assert any nonzero `ubitofu check`, malformed receipt, absent digest,
  digest mismatch, plan replacement, or backup failure prevents apply. Assert
  pre-health unavailability remains warning-only and post-health degradation
  makes the already-completed apply pipeline red.

- [ ] Confirm red:

  ```bash
  python3 -m unittest discover -s ci/tests -p 'test_unifi_ubitofu_contract.py' -v
  ```

- [ ] Implement the single-plan flow in the existing `unifi` branch of
  `ci/tofu.sh`. Remove the inline OpenTofu JSON high-risk parser; warnings now
  come from `ubitofu check`. Keep plan/apply behavior for other modules
  unchanged.

- [ ] Use Python stdlib only to read `outcome.inputs.plan_sha256` from the JSON
  receipt, then compare it with `sha256sum` output. This tiny boundary check is
  intentionally external: ubitofu does not own or execute apply.

- [ ] Remove the separate `unifi-apply-gate` Woodpecker step. Make
  `tofu-apply-unifi` depend directly on `lint-tofu-unifi`; the apply script now
  creates, checks, and applies one plan in one workspace/step. Update every
  lockstep path filter together.

- [ ] Delete `ci/unifi-gate.sh` only after the new contract tests and a
  credentialed check-only Woodpecker run pass. Verify no workflow references it.

- [ ] Run read-only local checks:

  ```bash
  bash -n ci/tofu.sh
  python3 -m unittest discover -s ci/tests -p 'test_unifi_ubitofu_contract.py' -v
  rg -n 'unifi-gate|reconcile --check|tofu show -json' .woodpecker ci
  git diff --check
  ```

  The remaining `tofu show -json` matches must belong to unrelated modules or
  documented tests; no UniFi safety policy parser remains.

- [ ] Commit:

  ```bash
  git add ci/tofu.sh .woodpecker/ci.yml \
    ci/tests/test_unifi_ubitofu_contract.py
  git add -u ci/unifi-gate.sh
  git commit -m "unifi: check and apply one exact saved plan"
  ```

---

### Task 5: Replace the health script and prune reconcile setup

**Files in `~/ansible`:**

- Modify: `ci/tofu.sh`
- Delete after green dual-run: `ci/unifi-health-check.sh`
- Modify: `ci/unifi-env.sh`
- Modify: `ci/unifi-reconcile.sh`
- Modify: `.woodpecker/unifi-reconcile.yml`
- Modify: `.woodpecker/ci.yml`
- Modify: `ci/tests/test_unifi_ubitofu_contract.py`

- [ ] Add contract assertions that `ci/tofu.sh` calls `ubitofu health snapshot`
  and `health compare`, interprets 0 as no degradation and 11 as degradation,
  and preserves the pre-snapshot-unavailable behavior. No shell or inline Python
  may parse controller health payloads.

- [ ] After health parity is recorded, delete `ci/unifi-health-check.sh` and all
  references. Keep only receipt-file lifecycle and pipeline alert routing in
  `tofu.sh`.

- [ ] Prune `ci/unifi-env.sh` to its honest external boundary: load the
  deployment/backend variables required by OpenTofu and initialize the module.
  Remove ubitofu installation, temporary config generation, forge credentials
  used only for installation, outcome messaging, and any controller-response
  interpretation.

- [ ] Run nightly reconcile from `infra/unifi` with the committed
  `ubitofu.toml`. Keep only exit-to-publication routing in
  `ci/unifi-reconcile.sh`: no change, captured diff, attention notification, and
  operational failure. Retain Git diff validation, drift-only branch creation,
  PR handoff marker, and notifications because those are consumer policy.

- [ ] Ensure `.woodpecker/unifi-reconcile.yml` still owns scheduling, secrets,
  marker gating, and PR creation. Update path filters to include the committed
  config and remove deleted scripts.

- [ ] Run:

  ```bash
  bash -n ci/tofu.sh ci/unifi-env.sh ci/unifi-reconcile.sh
  python3 -m unittest discover -s ci/tests -p 'test_unifi_ubitofu_contract.py' -v
  rg -n 'curl .*health|stat/health|json.load.*resource_changes|pip install.*ubitofu|reconcile.toml' \
    ci .woodpecker
  git diff --check
  ```

  Expected: no controller-health parser, plan-risk parser, runtime ubitofu
  install, or temporary config heredoc remains.

- [ ] Commit:

  ```bash
  git add ci/tofu.sh ci/unifi-env.sh ci/unifi-reconcile.sh \
    .woodpecker/unifi-reconcile.yml .woodpecker/ci.yml \
    ci/tests/test_unifi_ubitofu_contract.py
  git add -u ci/unifi-health-check.sh
  git commit -m "unifi: consume public health and reconcile outcomes"
  ```

---

### Task 6: Remove dual-run scaffolding and verify the thin consumer

**Files in `~/ansible`:**

- Delete: `ci/unifi-ubitofu-dual-run.sh`
- Modify: `.woodpecker/ci.yml`
- Modify: `.woodpecker/unifi-reconcile.yml`
- Modify: `ci/tests/test_unifi_ubitofu_contract.py`
- Modify: repository operator documentation that names the old commands/scripts

- [ ] Record the final parity evidence in the relevant private operator doc:
  ubitofu version, plan digest behavior, synthetic matrix result, live no-op/
  drift result, health result, pipeline identifiers, and rollback commits. Do
  not copy private evidence into the public ubitofu repository.

- [ ] Delete the temporary dual-run script and manual-only steps after all
  matrix rows have evidence. Retain the fast fake-command contract tests as the
  permanent consumer regression gate.

- [ ] Run the full read-only local gate:

  ```bash
  bash -n ci/tofu.sh ci/unifi-env.sh ci/unifi-reconcile.sh
  python3 -m unittest discover -s ci/tests -p 'test_*ubitofu*.py' -v
  ansible-lint --profile=production
  rg -n 'unifi-gate|unifi-health-check|unifi-ubitofu-dual-run|reconcile.toml' .
  git diff --check
  git status --short --branch
  ```

- [ ] Run credentialed Woodpecker verification in order:

  1. manual nightly reconcile with no drift;
  2. pull-request plan/check for an HCL-only fixture change;
  3. main apply path that produces no changes;
  4. approved low-risk exact-plan apply when one is naturally available;
  5. post-apply health comparison;
  6. nightly drift publication when a deliberate UI change is separately
     authorized.

  Inspect each with `woodpecker-cli pipeline show/ps/log`. A green pipeline is
  not proof until logs show the checked digest equals the applied plan digest,
  the backup completed before apply, and health comparison ran afterward.

- [ ] Request an adversarial review across both repositories focused on scope
  boundaries, secret flow, exact-plan binding, backup order, apply/no-op paths,
  post-apply alert semantics, path-filter DAG consistency, publication policy,
  and rollback.

- [ ] Commit cleanup:

  ```bash
  git add .woodpecker/ci.yml .woodpecker/unifi-reconcile.yml \
    ci/tests/test_unifi_ubitofu_contract.py
  git add -u ci/unifi-ubitofu-dual-run.sh
  git commit -m "unifi: retire the dual-run migration scaffolding"
  ```

## Completion Gate

Phase 6 is complete only when the permanent contract suite covers every parity
row, live Woodpecker evidence confirms the same plan digest was checked and
applied after a successful backup, health comparison retains delta semantics,
nightly reconcile still publishes only captured HCL diffs, all old UniFi plan/
health parsers and runtime install/config heredocs are gone, and no deployment,
Git, CI-vendor, backup, apply, or notification behavior has entered ubitofu.
