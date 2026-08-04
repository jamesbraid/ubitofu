# ubitofu Reconciliation Architecture — Adversarial Review

Status: historical review of the superseded 2026-08-02 design and plan.

Date: 2026-08-02
Reviewed artifacts:

- `2026-08-02-ubitofu-reconciliation-architecture-design.md`
- `../plans/2026-08-02-ubitofu-reconciliation-architecture-hardening.md`

Verdict on the first draft: **not ready to execute**

Verdict after revision: **direction accepted. Implementation remains blocked on
two feasibility gates**

## Method

An independent reviewer read the proposed design and plan against the current
`tofu_runner.py`, `pipeline.py`, `hcl_surgeon.py`, `controller.py`, `coverage.py`,
configuration, tests, mutation gate, and ignore rules. The review was read-only
and was explicitly asked to challenge the language, parser, state model,
transaction protocol, sequencing, and test claims.

The reviewer confirmed that the motivating defects are real:

- a failed generate-config plan can be accepted from a non-empty stub.
- Generate and reconcile write destination files before all work succeeds.
- fixed plan files survive some paths and are not covered by the existing ignore
  patterns.
- The HCL scanner can match resource-looking heredoc content.
- Controller coverage treats every 4xx response as endpoint absence.
- reconciliation policy is concentrated in the orchestration function and
  outside the per-PR zero-survivor mutation gate.

## Critical findings and dispositions

### The OpenTofu model excluded legal documents

The first plan required non-null `before` and `after` mappings, represented
replacement as a synthetic `REPLACE` action, omitted `forget`, and keyed
resources by type/name. OpenTofu's JSON contract instead permits null sides,
uses ordered create/delete vectors for replacement, and identifies instances by
absolute address plus module, index, mode, and optional deposed key.

Resolution: the revised model retains the full identity, exact action vector,
nullable values, and deep-frozen content. The initial editor supports only root,
managed, unindexed, current resources. Every other legal shape becomes explicit
attention rather than collapsing onto a root block.

Reference: [OpenTofu JSON output format](https://opentofu.org/docs/internals/json-format/).

### Failed-plan handling contradicted fail-closed behavior

The design failed to distinguish detailed-exitcode 2 from an actual failed plan,
while Task 1 preserved an exception for exit 1 when generate-config JSON looked
usable. OpenTofu's exit 1 is an error. A partial generated file can help diagnosis
but is not a sound reconciliation snapshot.

Resolution: only plan exit 0 or detailed-exitcode 2 plus `errored: false` can
produce planner input. The pinned provider-invalid case must be characterized.
If partial adoption is still valuable, it needs a separately designed
review-only mode that never commits output.

### The HCL position contract was not proven

The first plan used the transformed `hcl2.parses()` API and spoke of byte spans,
although python-hcl2 reports Python character positions. A local probe also
showed python-hcl2 8 rejecting CRLF accepted by OpenTofu. Current text I/O can
normalize newlines and violate byte preservation.

Resolution: parser selection is now a blocking corpus comparison. python-hcl2
uses `parses_to_tree()` plus a proven newline/character-to-byte map.
tree-sitter-hcl uses native byte ranges but must reject all recovered error or
missing nodes. A small Go `hclsyntax` helper is the final fallback, not a product
rewrite. No span engine ships unless it passes the same raw-byte corpus.

References:
[python-hcl2 API](https://raw.githubusercontent.com/amplify-education/python-hcl2/main/hcl2/api.py),
[OpenTofu files and UTF-8/newlines](https://opentofu.org/docs/language/files/).

### The crash guarantee exceeded the transaction protocol

The first draft promised old-or-complete-new after every handled error while
also acknowledging rollback failure. It did not define crash-safe journal
replacement, candidate/backup durability, metadata preservation, or recovery
from a corrupt journal.

Resolution: success yields complete new. Failed commit plus successful rollback
yields complete old. Rollback failure quarantines the transaction and blocks
future mutation while preserving backups. The protocol now requires an immutable
manifest, fsynced candidates/backups, atomically replaced and fsynced phase
markers, no-follow path operations, metadata checks, directory fsync, idempotent
recovery, and process-kill tests at every durable boundary.

### The staged module was incomplete and unproven

The first draft copied only `.tf` files and assumed a staged root could reuse
provider data. OpenTofu also loads `.tofu`, `.tf.json`, and `.tofu.json`, applies
extension precedence, requires installed providers/modules for validation, and
may depend on local modules outside the workdir. `TF_DATA_DIR` reuse against a
different root was not proven.

Resolution: staged validation is a blocking macOS/Linux spike. It must reproduce
the effective module, lockfile, and local-module graph, work offline, avoid
backend/provider APIs and destination writes, and fail with `initialize first`
when prerequisites are absent. If that cannot be demonstrated, pre-commit
`tofu validate` leaves the guarantee.

References:
[OpenTofu module file types](https://opentofu.org/docs/language/modules/),
[OpenTofu validate](https://opentofu.org/docs/cli/commands/validate/).

### “Safe stderr” was not safe

Provider errors can contain secret values, source excerpts, credential-bearing
URLs, and unbounded output. Labeling an excerpt safe does not sanitize it.

Resolution: normal exceptions expose an allowlisted command kind, status, and
safe reason only. Tests inject secrets, URL userinfo, ANSI control sequences,
large output, and HCL excerpts. Raw stderr is absent by default.

## Important findings and dispositions

- Parallel category lists and a stored exit code could contradict edit intents.
  The revised planner emits one `ResourceDecision` per observation and derives
  reports and outcomes from those decisions.
- Frozen dataclasses did not make nested JSON immutable. Boundary normalization
  now deep-freezes every retained value.
- Structural migration covered only `_locate()`. The revised scope includes
  committed addresses, imports, variables, and expression references. JSON
  configuration is discovered strictly but operator-owned JSON is not edited in
  the first release.
- File replacement omitted mode, inode, symlink-component, and platform rules.
  The transaction now captures `lstat` identity/mode, preserves existing modes,
  defines new-file mode, rejects symlinks with no-follow operations, and targets
  tested POSIX macOS/Linux behavior.
- `COVERAGE.md` and several generated outputs were outside the transaction. The
  renderer now enumerates every persistent destination before any write.
- Passing a CA path directly to HTTPX is deprecated. The revised design builds
  an `ssl.SSLContext`, and the secure-default change lands with README and
  changelog in an explicit breaking release.
- Generic 404/405 acceptance remained too broad. The design now requires an
  endpoint/dialect absence table and treats unlisted responses as failures.
- Parser rollout preceded transaction safety. It now follows in-memory rendering,
  staged validation proof, and durable transaction implementation.
- The mutation command and ignored-doc commit commands were not executable.
  The plan now uses the actual mutation CLI or adds a tested non-mutating mode,
  and force-adds only named architecture files when tracking is intentional.
- The proposed mutation scope did not explicitly retain current gates. The plan
  now lists the full retained and added scope.

## Alternative architecture verdict

- **Python vs Go/Rust:** keep Python. Boundary validation and filesystem
  protocol are the risks. A rewrite would discard mature fixtures without
  removing those risks.
- **Planner model:** use a pure `snapshot -> decisions` function. This is a
  finite classification, not a long-lived workflow.
- **State-machine library:** do not add one. The transaction needs a small
  explicit persisted state enum, not a framework.
- **HCL parser:** decide through the blocking corpus. python-hcl2 needs a byte
  map, tree-sitter needs fail-closed error handling, and a Go helper is a bounded
  fallback.
- **Multi-file commit:** keep a journaled protocol. Reconcile touches operator
  and generated files, so one output file or Git cannot provide the contract.
  Narrow the guarantee around rollback failure.

## Revised execution order

1. Baseline and decide document tracking.
2. Add locking, private artifacts, failed-plan rejection, and safe diagnostics.
3. Ship the TLS/HTTP policy as an explicit breaking release.
4. Add the exact OpenTofu boundary model.
5. Extract resource decisions and render all candidates in memory.
6. Prove offline staged validation.
7. Implement and kill-test durable recovery.
8. Compare/select the parser, then migrate lexical discovery.
9. Expand mutation gates without dropping current modules.
10. Run cross-platform, live-controller, and release verification.

## Final blocker pass

A second blocker-only review of the revised artifacts found five remaining
contract gaps, all corrected in place:

- The immutable transaction manifest now uses the same temporary-write, fsync,
  atomic-rename, and directory-fsync protocol as phase markers. Recovery tests
  include corrupt and truncated manifests.
- Acceptance now permits mode-0700 backup quarantine only after rollback failure
  and requires it to block later mutation.
- Process-kill tests now cover plan/stub residue, and the next locked startup
  safely reclaims orphaned scratch children.
- Mutation scope now includes the new lock and staged-validation modules.
- The mutation commit command enumerates intended tests instead of staging the
  entire `tests/` tree.

## Residual blockers

The revised direction is suitable, but coding should not start on the transaction
or structural editor until two facts are proven:

1. a faithful staged OpenTofu module can validate offline without touching the
   destination, backend, provider APIs, or registries.
2. one parser adapter returns exact original-byte spans and rejects malformed or
   partially recovered input for the complete corpus.

Those are evidence gates, not implementation details to assume away.
