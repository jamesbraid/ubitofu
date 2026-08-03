# ubitofu Reconciliation Architecture — Design

Date: 2026-08-02
Revised: 2026-08-03
Status: approved design direction, revised after adversarial review and bounded
feasibility spikes. The HCL parser gate selected tree-sitter-hcl. Offline staged
validation passed on macOS. Linux remains a blocking feasibility gate before
production rollout

## Product context

The durable product purpose and scope test live in `docs/product-brief.md`.
ubitofu enables administrators to use the UniFi UI or mobile app and committed
OpenTofu HCL as concurrent authoring surfaces. It converts live configuration
into reviewable HCL and stops code-driven workflows from silently overwriting
uncaptured controller changes.

CI consolidation is a consumer benefit of clear public interfaces. It is not
the product's purpose, and no CI vendor or repository workflow belongs in the
core.

## Architecture mission

Make `ubitofu reconcile` a fail-closed, transactional compiler pipeline while
preserving the product's defining constraint: it observes a live UniFi
controller and OpenTofu state, proposes reviewable HCL changes, and never
applies infrastructure changes itself.

The current Python implementation is a good fit for that mission. The change
is an internal architecture hardening, not a rewrite. It separates external
data acquisition, validation, decision-making, source editing, and filesystem
commit so each safety property can be tested independently.

## Why change the current architecture

The existing implementation has strong local pieces: forbidden OpenTofu
commands are guarded, HCL generation is deterministic, source edits preserve
comments, the CLI has useful outcome codes, and the default test suite has high
branch coverage. The main risk is how those pieces are composed.

`run_reconcile()` currently performs discovery, state reads, plan generation,
JSON interpretation, classification, HCL edits, reporting, and writes in one
large function. That creates four concrete failure modes:

1. A failed `tofu plan -generate-config-out` is accepted whenever it leaves a
   non-empty stub, even if the accompanying JSON plan says it is errored.
2. Reconcile and generate mutate files incrementally. A late validation or
   parsing failure can leave a partially updated worktree.
3. Binary plan files such as `tf.plan` and `verify.plan` can survive failed or
   successful runs. They may contain sensitive values and their names are not
   covered by the repository's current ignore rules.
4. The handwritten HCL scanner can match resource-looking text inside heredocs.
   Its lexical model is smaller than the HCL language it edits.

The monolithic orchestration also makes the highest-risk logic hard to mutation
test. Branch coverage is high, but the CI mutation gate deliberately excludes
`pipeline.py`, where most reconciliation decisions currently live.

## Constraints and non-goals

- Keep Python 3.11+. The workload is dominated by HTTP and subprocess I/O,
  document transformation, and operator-facing diagnostics. A Go or Rust
  rewrite would add migration risk without fixing the boundary and transaction
  problems.
- Keep Python as the implementation language. Select the HCL span engine through
  a bounded corpus comparison of python-hcl2, tree-sitter-hcl, and, only if both
  fail, a small Go `hclsyntax` helper. Do not rewrite the product around the
  parser choice.
- Do not add a state-machine framework. Reconciliation is a finite
  classification over immutable snapshots, not a long-lived event-driven
  process. Enums, frozen dataclasses, and total pure functions make the states
  explicit with less machinery.
- Do not reformat operator-owned `.tf` files. Edits remain byte-preserving
  outside explicitly selected spans.
- Do not move emulator/container lifecycle into ubitofu. Container harnesses
  remain test infrastructure. ubitofu owns adoption and reconciliation policy.
- Do not add controller writes, `tofu apply`, `tofu import`, or state mutation.
- Preserve existing commands and exit codes through a documented compatibility
  period while introducing the smaller command surface below.
- Do not add Git forge, CI vendor, backend backup, notification, or arbitrary
  deployment-secret integrations.

## Safety invariants

The target design is accepted only if all of these remain mechanically true:

1. **Read-only infrastructure:** every OpenTofu command passes the existing
   denylist guard. No controller mutation method is introduced.
2. **Fail closed:** any plan exit other than 0 or documented detailed-exitcode 2,
   malformed JSON, `errored: true`, or an unsupported JSON format major stops
   before any persistent file changes.
3. **No partial worktree:** all proposed file contents are computed and
   validated before the first destination file is replaced.
4. **Contained plan artifacts:** plan and generated-stub files live in a
   mode-0700 ignored scratch directory, are removed in `finally` paths, and are
   reclaimed under the workdir lock at the next startup after process death.
5. **Decision parity:** `reconcile --check` and normal reconcile build the same
   `ReconcilePlan`. Only the final commit step differs.
6. **Anchored edits:** an edit names a resource address, attribute, expected old
   literal, and replacement. A missing or ambiguous anchor is an error.
7. **Byte preservation:** applying no edits returns identical bytes. Applying
   an edit changes only its selected span.
8. **Validated boundaries:** external JSON and provider schemas are deep-frozen
   into typed domain values before planner code sees them. Raw or nested mutable
   mappings do not cross into the decision layer.
9. **Secure transport by default:** TLS verification defaults on. Disabling it
   requires explicit configuration. A custom CA bundle is supported.
10. **Actionable failure:** diagnostics identify the phase and resource without
    printing raw provider stderr, credentials, URLs with userinfo, response
    bodies, plan contents, or secret-bearing HCL excerpts.
11. **Single writer:** a workdir-scoped advisory lock is held from snapshot
    acquisition through commit or check completion. Hash checks still protect
    against tools that do not honor the lock.
12. **Plan binding:** a safety decision for a code-driven workflow names the
    digest of the exact saved plan inspected. An external apply system must use
    that same plan file.
13. **No implicit winner:** compatible UI and HCL changes merge. Incompatible
    changes to the same managed value become conflicts. Neither side wins by
    default.

## Target architecture

The pipeline has six layers with one-way dependencies:

```text
Controller adapter       OpenTofu adapter       HCL span engine
        |                       |                   |
        +---------- validated snapshots ---------+
                                |
                         pure planner
                                |
                        ReconcilePlan
                                |
                  render edits in memory/staging
                                |
                    tofu fmt + validate staged tree
                                |
                      atomic filesystem commit
```

### 1. External adapters

`controller.py` remains the HTTP adapter. It owns authentication, URL dialect,
TLS policy, HTTP status handling, and response-envelope validation. Collection
callers receive validated lists or a typed error. Authentication and rate-limit
responses are never interpreted as an absent endpoint.

`tofu_runner.py` remains the only subprocess boundary. It owns command safety,
mode-0700 scratch artifacts, exit-code interpretation, JSON decoding, and format
version validation. Higher layers request a plan snapshot, state snapshot, or
schema snapshot. They do not manage plan filenames. Exit 0 and the documented
`-detailed-exitcode` value 2 are the only plan results that can yield a usable
snapshot, and the JSON must also say `errored: false`. Partial generated HCL from
exit 1 is an ephemeral diagnostic artifact, never planner input.
Non-plan commands such as `show`, `validate`, and `providers schema` require
exit 0. The adapter does not apply plan exit semantics globally.

### 2. Validated snapshots

A new `tofu_json.py` module converts OpenTofu JSON into the subset the product
uses. It validates:

- `format_version` exists and has a supported major version.
- Plan documents are not marked `errored`.
- the opaque absolute address, module address, mode, type, name, instance index,
  and optional deposed key have expected shapes.
- action vectors exactly match a supported OpenTofu vector: `no-op`, `create`,
  `read`, `update`, `delete`, `forget`, delete-then-create, or
  create-then-delete.
- `before` and `after` values have the nullable shapes required by their action.
- Planned root-module resources are a sequence of resources.
- provider schemas contain the provider/resource/block maps used downstream.

The boundary may use small `TypedDict`s while decoding, but it deep-freezes every
retained list and mapping into tuples and domain records. An
`OpenTofuAddress` retains the full identity rather than reconstructing it from
type and name. Root, unindexed, current managed resources are the only edit
targets supported initially. Module, indexed, data-mode, and deposed changes are
reported as unsupported attention items before planning. They are never
collapsed onto a root resource. Unknown fields remain forward-compatible and
are ignored. Incompatible major versions and missing required fields fail.

Controller discovery and committed HCL are likewise normalized into immutable
snapshots. Schema-dependent cleaning and lifecycle extraction happen during
snapshot construction, so the planner does not traverse a provider-schema
mapping or call rendering helpers. Snapshot collection may perform I/O, but it
never writes destination HCL.

### 3. Domain model and pure planner

`reconcile_model.py` defines the vocabulary:

- `OpenTofuAddress(absolute, module, mode, type, name, index, deposed)`
- `ActionVector`: exact supported action tuples, including both replacement
  orders and `FORGET`
- `Divergence`: `DELETED`, `PENDING`, `ORPHANED`, `FORBIDDEN_CREATE`
- edit intents: `UpdateScalar`, `DeleteResource`, `AppendResource`,
  `AppendImport`, and `DeclareVariable`
- `AttentionItem` with a stable machine-readable reason and display message
- `ResourceDecision` containing one observation's disposition, safe reason, and
  zero or more edit intents
- `ReconcilePlan` containing ordered decisions and truly global edits

`reconcile_planner.py` implements a total function resembling:

```python
def build_reconcile_plan(snapshot: ReconcileSnapshot) -> ReconcilePlan: ...
```

The function has no filesystem, HTTP, subprocess, clock, environment, or output
dependencies. It turns a complete snapshot into an immutable plan. It sorts all
externally sourced collections before emitting decisions so identical snapshots
produce equal plans.

Reports, category lists, and the exit code are derived from the ordered
`ResourceDecision` values. They are not stored as parallel mutable-looking
summaries that can contradict the disposition or edits.

The planner performs a semantic three-way merge using committed HCL, OpenTofu
state, and the live controller. Independent resource or attribute changes
combine. Equal resulting values converge. Provider-computed values and planned
unknowns do not become false drift. Different changes to the same managed value,
delete-versus-modify cases, and structures without stable identities become
explicit conflicts or attention items. Positional-list guesswork is forbidden.

This is where classification lives. There is no state-machine engine: the
cartesian product of plan action, live identity, state identity, committed
block, lifecycle policy, and last-applied value is explicit in tables and
parameterized tests. New combinations must either produce a decision or raise a
typed `UnsupportedReconcileCase`. Silent fall-through is forbidden.

### 4. Structural HCL editing

The bounded parser spike selected tree-sitter-hcl 1.2.0 with tree-sitter 0.26.0.
A proof corpus compared:

- python-hcl2's positioned raw Lark tree through `parses_to_tree()`.
- tree-sitter-hcl with any `ERROR` or `MISSING` node treated as a hard failure.
- a small Go helper using HashiCorp's `hclsyntax` parser only if neither Python
  option satisfies the contract.

The python-hcl2 adapter handled CRLF normalization and exact original-byte
mapping, but its raw Lark tree exposed a heredoc as an opaque token and missed a
qualified reference inside interpolation. tree-sitter-hcl returned that span
and matched all 53 literal expected block, attribute, and qualified-reference
spans across 8 valid cases. Both candidates rejected all 3 invalid cases.

Tree-sitter's native byte ranges avoid the coordinate map, but its error recovery
is unsafe unless the adapter rejects every recovered or missing node. The proof
walks the complete tree and fails on every `ERROR` or `MISSING` node. The
decision evidence and remaining rollout gate are recorded in
`docs/hcl-parser-decision.md`.

Edits still splice the original source rather than reconstructing it. This
retains comments and formatting while eliminating the scanner's false matches.
Every edit verifies its expected old text. Multiple edits to one file are
checked for overlap and applied from the highest offset downward.

A serializer such as `hclwrite` remains unsuitable for operator-owned files
because whole-document reconstruction would violate byte preservation.

The selected index replaces resource-block discovery, committed-address and
import discovery, and variable-declaration discovery. Dangling-reference checks
use indexed expressions and deliberately exclude comments and string literals.
Any retained lexical approximation is named in the design with its false-positive
policy and a regression test.

Native `.tf` and `.tofu` files use the selected span engine. JSON configuration
is parsed with a strict JSON loader for address/preference discovery. Existing
operator-owned `.tf.json` or `.tofu.json` resources are classified but not
byte-edited in the first release. Drift that would require such an edit is an
attention result. ubitofu-owned JSON output may be regenerated transactionally.
The module manifest honors OpenTofu's extension-precedence rules so a shadowed
`.tf` file is never treated as the active edit target.

### 5. Staged validation and commit

Before transaction implementation, a blocking real-OpenTofu spike must prove a
faithful, offline staged module on macOS and Linux. Its manifest includes `.tf`,
`.tofu`, `.tf.json`, `.tofu.json`, precedence/override variants,
`.terraform.lock.hcl`, and every resolved local-module source needed for
validation. It must reuse already-installed providers and modules without
backend access, provider API calls, registry downloads, or destination writes.
Missing prerequisites fail with an `initialize first` diagnostic. If those
properties cannot be proven, mandatory pre-commit `tofu validate` is removed
from the guarantee rather than implemented as a best-effort check.

Once the gate passes, `file_transaction.py` materializes every destination in
the `ReconcilePlan` into a mode-0700 private staging directory on the same
filesystem as the workdir. Destinations include operator edits, generated/import
files, variable declarations, removals, and `COVERAGE.md`. It copies rather than
hardlinks any input that a validation or formatting command might write.

Before commit it runs:

1. internal edit validation (anchors, non-overlap, expected hashes).
2. `tofu fmt -check` on generated files, or formatting before their final hash.
3. `tofu validate` against the proven offline staged module.
4. a recheck that destination files still match the hashes captured at snapshot
   time.

The transaction protocol is a small explicit persisted state machine, implemented
directly rather than through a framework. An immutable manifest records relative
paths, original/candidate/backup hashes, original `lstat` identity and mode, and
the protocol version. The manifest itself is written to a new temporary file,
fsynced, atomically renamed, and followed by a transaction-directory fsync before
mutation. Candidates and complete private backups are fsynced first. Each phase
marker follows the same temp-write, fsync, atomic-rename, directory-fsync
protocol. Destination operations use no-follow path validation, preserve
existing file modes, assign a documented mode to new files, and fsync affected
parent directories.

Python and POSIX do not provide a single atomic transaction across several
files. The guarantee is therefore narrower. Success yields the complete new
tree. A failed commit with a successful rollback yields the complete original
tree. A rollback failure quarantines the transaction, preserves backups, blocks
future mutation, and prints manual recovery paths. Startup recovery is
idempotent for every durable phase and stops on corrupt/truncated manifests or
markers, multiple residual transactions, hash ambiguity, or external modification.
Process-kill tests cover every durable boundary rather than simulating crashes
only with raised exceptions.

A kernel advisory workdir lock covers snapshot acquisition, staging,
validation, and commit on supported POSIX platforms. A symlinked `.ubitofu` or
symlink in any destination path is rejected. The pre-commit identity, mode, and
hash check remains mandatory because editors and other tools do not honor this
lock. `--check` stops after building and validating the same staged plan. It
prints the same report and exit code but never starts the commit phase.

### 6. Thin orchestration

`pipeline.py` remains the command-level composition root. `run_reconcile()` is
reduced to:

1. acquire controller, OpenTofu, schema, state, plan, and source snapshots.
2. build the pure `ReconcilePlan`.
3. stage and validate it.
4. commit unless `--check`.
5. render the report from the plan.
6. close resources and remove scratch data in `finally` blocks.

Generate and verify reuse the OpenTofu artifact manager and JSON validation.
Generate may have a separate rendering plan, but it follows the same
stage/validate/commit rule. Verify never creates a persistent plan file.

## Public command surface

The preferred command model is:

```text
ubitofu generate   --config ubitofu.toml
ubitofu reconcile  --config ubitofu.toml
ubitofu check      --config ubitofu.toml --plan tfplan.out
ubitofu inspect    --config ubitofu.toml
ubitofu health snapshot --config ubitofu.toml
ubitofu health compare  --config ubitofu.toml --before health.json
```

`generate` bootstraps HCL from a live controller. `reconcile` performs the
semantic merge and commits validated candidate HCL. `check` is read-only and
authorizes or rejects the supplied saved plan. `inspect` exposes controller and
provider coverage. `health` replaces consumer-side UniFi health parsing without
running an apply.

Every command supports human output and a versioned JSON envelope through
consistent `--format` and `--output` options. Operational diagnostics go to
stderr. Primary human or JSON output goes to stdout or the requested file.
Receipts contain stable reason codes and input digests but no sensitive values.
No `ci` namespace or vendor-detection behavior is introduced.

`enumerate`, `verify`, and `reconcile --check` remain compatibility entry points
for one major-version cycle. They move onto the same snapshot and planner core
before deprecation. `check` adds the stronger saved-plan authorization contract
rather than pretending the old commands already provide it.

## Integration boundary

A generic code-driven workflow remains explicit:

```text
tofu init
tofu plan -out=tfplan.out
ubitofu check --config ubitofu.toml --plan tfplan.out
ubitofu health snapshot --config ubitofu.toml --output health.json
external state backup
tofu apply tfplan.out
ubitofu health compare --config ubitofu.toml --before health.json
```

The external system owns initialization, state backup, and apply. Using the same
saved plan for `check` and `apply` closes the current gap where a gate can inspect
one plan and apply another.

A controller-driven workflow is equally ordinary:

```text
ubitofu reconcile --config ubitofu.toml
git diff
external commit, push, pull request, and notification
```

ubitofu does not know whether either sequence runs from a terminal, Makefile,
Woodpecker pipeline, or GitHub Action.

### Ansible consumer migration

The Ansible repository becomes a thin deployment-policy consumer:

- `ci/unifi-gate.sh` is replaced by `ubitofu check`.
- `ci/unifi-health-check.sh` is replaced by `ubitofu health`.
- ubitofu configuration, installation, and outcome policy leave
  `ci/unifi-env.sh`. Deployment secrets remain outside ubitofu.
- `ci/unifi-reconcile.sh` retains only Git publication of a produced HCL diff.
- UniFi plan-risk and health parsing leave `ci/tofu.sh`. Initialization, R2
  state backup, and apply remain.
- Woodpecker retains event routing, path filters, PR creation, and Pushover.

Cutover uses dual-run evidence. Existing gates and new commands run side by side
until they agree on no-op, controller drift, code changes, compatible concurrent
changes, true conflicts, device creation, and health degradation. Shell code is
removed only after the corresponding public command passes those cases.

## Error model

Errors cross layers as typed exceptions carrying safe context:

- `TofuExecutionError(command_kind, exit_code, safe_reason)`
- `TofuDocumentError(kind, reason)`
- `ControllerResponseError(endpoint, status, reason)`
- `HclIndexError(path, reason)`
- `StaleSnapshotError(path)`
- `TransactionError(phase, affected_paths, recovery_path)`

The CLI maps all operational failures to exit code 1. Existing reconcile
outcome codes 10, 11, 12, and the forbidden-create code remain non-error
results. Reports are rendered only from a successfully built plan. Adapter logs
may retain raw stderr only behind an explicit local debug opt-in and restrictive
permissions. Normal error text is allowlisted and sanitized. Tests cover API
keys, passwords, secret values, URLs with userinfo, ANSI escapes, and oversized
provider output.

## TLS and controller response policy

Configuration adds `verify_tls` and optional `ca_bundle`. A custom bundle is
loaded with `ssl.create_default_context(cafile=...)` and the controller receives
either a boolean or `ssl.SSLContext`, avoiding HTTPX's deprecated string
`verify=` interface. Existing installations using self-signed certificates must
explicitly choose `verify_tls = false` or provide their CA.

The default changes from false to true only in an explicit breaking release.
The configuration, warning/migration path, README, and changelog land together.
It is not described as an independently releasable compatibility-preserving
step.

Endpoint coverage probing distinguishes:

- an endpoint-and-dialect policy table's explicit absence response: endpoint
  absent.
- 401/403: authentication or authorization failure.
- 429 on idempotent GET: rate limit, with numeric or HTTP-date `Retry-After`, an
  attempt limit, and a total deadline.
- Other 4xx/5xx: operational failure.
- successful malformed JSON/envelopes: contract failure.

Only the first category contributes an accepted coverage gap. An unlisted 404 or
405 is an operational failure, not evidence of absence.

## Compatibility and migration

The migration is incremental, with one deliberate breaking release:

1. baseline current outputs and decide whether these architecture artifacts are
   tracked.
2. add the workdir lock, private artifact manager, strict failed-plan handling,
   and safe diagnostics.
3. add TLS configuration and response policy in an explicit breaking release
   with its migration note.
4. introduce the exact OpenTofu model and deep-frozen snapshots.
5. extract one `ResourceDecision` per observation and render every candidate in
   memory before any write.
6. prove faithful offline staged validation across macOS and Linux.
7. implement and process-kill-test the durable transaction protocol.
8. run the parser comparison, select the span engine, and replace all relevant
   lexical discovery behind transaction protection.
9. mutation-test each new safety module while retaining existing mutation scope.
10. add versioned JSON receipts, `check`, `inspect`, and `health` without removing
    existing commands.
11. dual-run the Ansible gates against the public commands and compare decisions.
12. bind the apply gate to the exact saved plan, cut over the consumer, and
    remove replaced UniFi-specific shell policy.
13. remove compatibility aliases only in the next major release.

Each non-breaking step is independently releasable. Compatibility fixtures
compare old and new reports, exit codes, and output bytes for supported inputs.
Deliberate behavior changes have explicit tests: errored plans fail, heredoc
decoys are ignored, partial writes roll back, and TLS verifies unless disabled.

## Verification strategy

- Unit tests exhaust the planner's decision table and transaction failure
  points without mocks of planner logic.
- Property tests generate action/identity/config combinations and assert that
  every input has one stable disposition.
- HCL corpus tests compare parser candidates across comments, heredocs,
  interpolation, nested blocks, duplicate-looking text, mixed newlines, Unicode,
  and byte preservation.
- Adapter tests exercise malformed and future-major OpenTofu JSON plus all
  relevant HTTP status classes.
- Integration tests use the real `tofu` binary against temporary modules to
  verify plan cleanup and staged validation.
- Existing container-controller scenarios prove live compatibility.
- The per-PR mutation gate retains `enumerator.py`, `import_emitter.py`, and the
  selected HCL editor, and adds `reconcile_planner.py`, `tofu_json.py`, and
  `file_transaction.py`. `pipeline.py` leaves mutation scope only after a checked
  thin-shell complexity threshold and keeps branch coverage.

## Acceptance criteria

The architecture is complete when:

- no command leaves `*.plan`, `tf.plan`, `verify.plan`, or generated stubs after
  success or handled failure. Startup reclaims private artifact residue left by
  process death.
- No transaction backup remains after success or successful rollback. A rollback
  failure is the explicit exception: it preserves a mode-0700 quarantine for
  recovery and blocks later mutation.
- A plan exit other than 0/2 or a JSON plan marked `errored` cannot produce
  destination edits.
- process termination at each durable transaction boundary either recovers to a
  verified complete tree or quarantines mutation with recoverable backups.
- `--check` and normal reconcile produce equal `ReconcilePlan` values from the
  same snapshot.
- the selected HCL engine passes the proof corpus, and a heredoc containing
  a fake resource cannot redirect an edit.
- Unsupported OpenTofu JSON major versions fail with an actionable message.
- TLS is verified by default and auth/rate-limit errors are not reported as
  unsupported endpoints.
- planner/transaction mutation tests have no unexplained survivors in the
  per-PR gate.
- the offline staged-validation gate passes on macOS and Linux without backend,
  provider API, registry, or destination access.
- independent UI and HCL changes merge while incompatible changes to the same
  managed value fail with base, HCL, and controller context.
- `check` authorizes the exact saved plan later supplied to external `tofu
  apply` and never generates a replacement plan.
- human reports, JSON receipts, and exit codes derive from the same ordered
  decisions.
- no Git, CI-vendor, backend backup, apply, or notification behavior enters the
  ubitofu package.
- the Ansible nightly reconcile, pre-apply gate, and post-apply health behavior
  survive cutover with their repository-specific publication and deployment
  policy intact.
- Ruff, strict mypy, branch coverage, property tests, default integration tests,
  and the selected live-controller scenarios pass.

## Rejected alternatives

### Rewrite in Go or Rust

Both could provide stronger compile-time modeling,
but the dangerous boundaries would still require validation and transactional
design. Rewriting the mature HCL/report/test behavior is higher risk than
isolating it behind typed Python modules.

### General state-machine library

It would add transition vocabulary and runtime machinery to the snapshot-to-plan
calculation. A total pure function plus decision-table tests exposes invalid
combinations more directly. The persisted transaction protocol does have an
explicit state enum and transition table, but it does not need a framework.

### Select a parser by preference

Neither the installed dependency nor tree-sitter wins by default. python-hcl2
avoids a new dependency but needs newline and coordinate adaptation.
tree-sitter-hcl offers byte ranges but needs strict error-node rejection and
native packaging. The corpus gate makes that tradeoff measurable.

### Parse and re-emit all HCL

It simplifies mutation but destroys comments and
operator formatting, which are part of the product contract.

### Git as the transaction layer

Requiring a clean Git repository would make
the library less composable and still would not protect non-Git workdirs. Git
remains the operator review surface, not an internal correctness dependency.

### Full deployment orchestration

Moving `tofu apply`, state backup, Git publication, and notification into
ubitofu would shorten one consumer's scripts by giving the public tool backend,
forge, CI, and deployment credentials. It would weaken the read-only safety
promise and replace shell maintenance with a larger plugin and recovery surface.
Those operations remain adjacent Unix tools.

### Generate CI templates

Generated Woodpecker or GitHub configuration would preserve two sources of
workflow policy and couple releases to vendor syntax. Stable commands and JSON
receipts give every CI system a smaller interface without making ubitofu a
template generator.
