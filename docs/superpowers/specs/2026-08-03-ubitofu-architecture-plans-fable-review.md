# Fable Review: ubitofu Architecture and Implementation Plans

Date: 2026-08-03
Reviewer: Fable
Scope: product brief, high-level architecture, proof results, six implementation
phases, current ubitofu code, and the adjacent Ansible consumer
Verdict: revise before implementation

This was a read-only documentation and code review. No tests were run.

## Architecture verdict

The product definition is strong, the Unix-tool boundary is clear, and Python
with frozen domain records and pure functions remains the right implementation
choice. A Go or Rust rewrite and a state-machine framework would add migration
and maintenance cost without addressing the main risks. The dependency
direction is sound:

```text
adapters -> immutable snapshots -> pure planner -> rendering -> validation -> commit
```

The plans are not ready to execute. Three central guarantees are either
underspecified or operationally unrealistic:

1. Mandatory staged validation requires host capabilities that ordinary Unix
   and CI environments often lack, while reconstructing a material part of
   OpenTofu's loader and cache semantics.
2. Saved-plan digest binding prevents plan substitution but does not prevent a
   controller change between check and apply.
3. The transaction plan does not specify filesystem operations precisely enough
   to support its symlink, race, and crash-recovery guarantees.

These findings require a focused architecture errata, not a redesign. Do not
start Phase 1 as written because its workdir authority and OpenTofu compatibility
decisions constrain all later phases.

## Strengths

- The durable scope test keeps controller writes, apply, backend management,
  Git and forge operations, CI routing, notifications, and deployment secrets
  outside ubitofu. See `docs/product-brief.md:44-85`.
- Python and the no-framework decision fit an I/O-heavy document transformation
  tool. The persisted transaction may use an explicit enum without turning
  semantic reconciliation into a runtime workflow engine. See
  `docs/superpowers/specs/2026-08-02-ubitofu-reconciliation-architecture-design.md:58`
  and `:554`.
- Complete addresses, unsupported expanded identities, deep-frozen inputs, one
  decision per observation, and decision-derived reports and exits directly
  address the current monolith's risks. See the architecture design at `:157`.
- The transaction guarantee is narrower and more honest than multi-file
  atomicity. Process-kill testing at durable boundaries is the right test
  strategy. See the architecture design at `:286`.
- The tree-sitter decision is evidence-based and correctly waits for transaction
  protection. See `docs/hcl-parser-decision.md:3-16` and `:103-109`.
- The Ansible ownership split matches the existing consumer. OpenTofu apply and
  backup remain external, while nightly reconcile retains publication and
  notification policy. See
  `docs/superpowers/plans/2026-08-03-06-ansible-cutover.md:48-60`.

## Critical findings

### 1. Hardened staged validation is not a portable small-tool default

The proposed production path requires an initialized dependency cache, rejects
all `file*` uses and several local-module forms, reconstructs active source
precedence, and filters lockfiles, module metadata, and provider trees. It then
refuses to run without macOS sandbox enforcement or Linux user and network
namespaces. The Linux proof required additional namespace capability, while
normal CI deliberately lacks it.

Evidence:

- `docs/staged-validation-contract.md:7-61`
- `docs/staged-validation-contract.md:99-112`
- `docs/staged-validation-contract.md:152-162`
- `docs/staged-validation-contract.md:202-207`
- `docs/superpowers/plans/2026-08-03-03-transactional-editing.md:24-45`
- `docs/superpowers/plans/2026-08-03-03-transactional-editing.md:49-196`
- `docs/superpowers/plans/2026-08-03-06-ansible-cutover.md:64-112`

Why it matters: a general open-source `reconcile` command would fail on common
hardened hosts and ordinary containers. The effective-module reconstruction
also risks becoming a second, incomplete OpenTofu loader.

Required decision: write a portability and threat-model ADR, then choose one of
these contracts:

- Treat hardened sandbox capability as a supported-platform prerequisite. Add
  a capability command and provision every consumer runner accordingly.
- Split portable structural and transactional validation from an optional
  hardened real-OpenTofu lane.
- Isolate the OS sandbox in a small helper or sidecar while delegating module
  resolution to OpenTofu instead of duplicating it.

Do not promote the proof wholesale into the mandatory runtime path.

### 2. Exact-plan binding does not fulfill the controller-overwrite promise

The product promises to stop code workflows from silently overwriting
uncaptured controller changes. The proposed receipt binds a decision to exact
plan bytes, and the consumer rehashes those bytes before apply. That prevents
plan substitution. It does not detect a controller change after `check` and
before `apply`.

Evidence:

- `docs/product-brief.md:14-18`
- `docs/product-brief.md:39-42`
- `docs/superpowers/specs/2026-08-02-ubitofu-reconciliation-architecture-design.md:113-118`
- `docs/superpowers/specs/2026-08-02-ubitofu-reconciliation-architecture-design.md:374-390`
- `docs/superpowers/plans/2026-08-03-05-public-interfaces.md:157-237`
- `docs/superpowers/plans/2026-08-03-05-public-interfaces.md:469-489`
- `docs/superpowers/plans/2026-08-03-06-ansible-cutover.md:226-251`

Why it matters: the central safety promise remains false during ordinary
concurrent UI or mobile use.

Required fix: define authorization as point-in-time. Bind the receipt to the
plan, source module, provider schema, and canonical controller snapshot, or to a
controller revision token when one exists. Require a final check immediately
before apply and define a maximum receipt age. If the controller exposes no
usable revision, document the residual race and narrow the absolute promise.

### 3. The transaction protocol is not race-safe enough for its guarantees

The design requires no-follow validation and source identity rechecks, but the
plan still describes pathname validation followed by same-directory temporary
files and pathname-based `os.replace`. It does not require pinned directory
descriptors, relative `*at` operations, durable temporary names, or recovery of
a crash between temporary-file creation and replacement. It promises mode
preservation without deciding how to handle ownership, ACLs, extended
attributes, or file flags.

Evidence:

- `docs/superpowers/specs/2026-08-02-ubitofu-reconciliation-architecture-design.md:300-325`
- `docs/superpowers/plans/2026-08-03-03-transactional-editing.md:217-304`
- `docs/superpowers/plans/2026-08-03-03-transactional-editing.md:325-380`
- `src/ubitofu/pipeline.py:739-815`
- `src/ubitofu/pipeline.py:872-906`

Required fix: specify the syscall-level protocol before Phase 1 establishes its
lock abstraction. Hold a canonical workdir directory descriptor. Resolve every
parent with no-follow traversal. Use directory-relative create, rename, and
unlink operations. Put every temporary name in the durable manifest. Define
startup handling for orphaned temporary files and choose the supported metadata
preservation contract explicitly.

## Important findings

### 1. The semantic base, desired, and live values are not defined

`ResourceObservation` contains source attributes, prior state, live values, a
plan change, lifecycle, and `stable_collection_keys`. The plans never define
which value represents HCL intent when HCL contains expressions. They also do
not define where stable element keys originate. Provider schema describes
collection shape but does not necessarily identify business keys.

Evidence:

- `docs/superpowers/plans/2026-08-03-02-semantic-planner.md:152-207`
- `docs/superpowers/plans/2026-08-03-02-semantic-planner.md:358-372`
- `src/ubitofu/pipeline.py:218-229`
- `src/ubitofu/pipeline.py:686-707`

Required fix: define normalized `base`, `desired`, and `live` fields. For every
action, state whether desired comes from plan `after`, parsed source literals,
or another source. Add a provider and resource policy registry for stable
collection identity. Treat collections without an explicit identity policy as
atomic attention or conflict.

### 2. `check` both reuses a plan-owning collector and promises never to plan

Phase 2 gives the reconcile collector a `TofuRunner` and responsibility for a
complete snapshot. Phase 5 says `check` collects that snapshot while also
requiring proof that it never calls `tofu plan`.

Evidence:

- `docs/superpowers/plans/2026-08-03-02-semantic-planner.md:195-207`
- `docs/superpowers/plans/2026-08-03-05-public-interfaces.md:200-234`

Required fix: split collection into plan-independent observations plus an
injected `PlanDocument`. Reconcile supplies its ephemeral plan. `check` supplies
only the caller's saved plan and never reads or generates another plan.

### 3. Generic exits do not distinguish warning, permission, and block

The common outcome model maps every command onto 0, 10, 11, 12, and 13. `check`
includes ordinary planned changes and high-risk warnings, while Phase 6 blocks
apply on every nonzero `check` result. The current consumer treats high-risk
changes as advisory.

Evidence:

- `docs/superpowers/plans/2026-08-03-05-public-interfaces.md:49-112`
- `docs/superpowers/plans/2026-08-03-05-public-interfaces.md:219-237`
- `docs/superpowers/plans/2026-08-03-06-ansible-cutover.md:175-187`
- `docs/superpowers/plans/2026-08-03-06-ansible-cutover.md:235-251`

Required fix: publish a per-command exit table before implementing receipts.
For `check`, safe changes and advisory warnings need an allow exit. Blocking
drift or conflict and forbidden creation need distinct nonzero results. Do not
make consumers infer authorization from `OutcomeKind.CHANGED`.

### 4. OpenTofu and runtime support boundaries are missing

Phase 1 validates JSON format major without declaring a supported OpenTofu CLI
range. Phase 3 assumes OpenTofu 1.12.x, while the proof covers only 1.12.0 on two
arm64 environments. Phase 4 adds native dependencies but leaves the packaging
matrix conditional.

Evidence:

- `docs/superpowers/plans/2026-08-03-01-safety-foundation.md:111-163`
- `docs/superpowers/plans/2026-08-03-03-transactional-editing.md:19-22`
- `docs/staged-validation-contract.md:1-5`
- `docs/superpowers/plans/2026-08-03-04-structural-hcl-index.md:46-66`

Required fix: Phase 1 must parse `tofu version -json`, declare minimum and
maximum tested CLI versions and JSON format majors, and expose a capability
report. Phase 4 needs an exact Python-version, OS, architecture, and wheel/sdist
matrix plus an unsupported-platform policy.

### 5. Phase 2 is not independently releasable with the legacy writer

The roadmap calls each non-TLS phase independently releasable. Phase 2 switches
reconciliation to a new planner but deliberately retains direct writes until
Phase 3.

Evidence:

- `docs/superpowers/plans/2026-08-03-ubitofu-architecture-roadmap.md:46-50`
- `docs/superpowers/plans/2026-08-03-02-semantic-planner.md:11-15`
- `docs/superpowers/plans/2026-08-03-02-semantic-planner.md:36-38`
- `docs/superpowers/plans/2026-08-03-02-semantic-planner.md:467-479`

Required fix: make Phases 2 and 3 one release unit, or put the new planner behind
check-only or feature-gated wiring until transaction commit is ready. Do not
release the new wet path through `_commit_candidates_legacy`.

## Cross-cutting gaps

The following requirements are missing across all six plans:

- temporal validity for controller observations between plan check and apply
- an OpenTofu CLI, Python, OS, architecture, and capability matrix
- an explicit source of stable map and set element identity
- a syscall-level path-authority and filesystem-metadata contract
- receipt evolution rules for required and optional fields, unknown fields,
  reason-code stability, and command-specific exit compatibility.

The plans also duplicate or overload several concepts:

- Phase 3 builds effective-source and precedence logic, then Phase 4 replaces it
  with `module_index.py`. One release would contain two interpretations that can
  drift.
- `attention` means reconcile finding, health degradation, plan warning, and
  possible plan blocker without a command-specific authorization table.
- Phase 1 validates plan headers, while Phase 2 performs exact modeling. Calling
  Phase 1 a complete trustworthy-boundary gate overstates how much untrusted
  JSON has been normalized.

## Recommended phase changes

### Phase 0: settle the cross-phase contracts

Add a short architecture errata covering:

- supported OpenTofu, Python, platform, and capability matrix
- staged-validation threat model and portability policy
- point-in-time saved-plan authorization
- exact base, desired, and live semantics plus collection identity policy
- directory-descriptor-based transaction protocol and metadata guarantees
- command-specific exits and receipt compatibility.

### Phase 1A: narrow the safety foundation

Land safe diagnostics, command-specific exit handling, JSON rejection,
OpenTofu version and capability detection, private artifacts, and a lock that
returns pinned filesystem authority rather than only `Path` values.

Ship the TLS default change separately as a named breaking release.

### Phase 2 and Phase 3: one release unit

Build immutable snapshots and the pure planner behind check-only or a feature
gate. Add the transaction and one shared effective-source component before
enabling the new wet path. Decide whether hardened real-OpenTofu validation is a
mandatory platform tier or an optional gate.

### Phase 4 onward

Keep structural parsing after transaction protection. Replace the conditional
packaging language with an exact support matrix and broaden the corpus.

Land outcome and receipt compatibility before saved-plan check. Inject the
caller's plan document into check and require live revalidation immediately
before apply.

Keep the Ansible cutover last. Add capability preflight and preserve the
difference between advisory warnings and blockers. Keep apply, backup, Git and
PR behavior, secrets, CI routing, and notifications outside ubitofu.

## Readiness

Phase 1 is a no-go as written. Its diagnostic and failed-plan tasks are locally
actionable, but the lock abstraction would bake in rework before the filesystem
authority contract is decided. The OpenTofu boundary also needs a version and
capability policy.

Resolve Phase 0 first. After that focused errata, the chosen architecture should
be ready to implement without a language rewrite or state-machine framework.
