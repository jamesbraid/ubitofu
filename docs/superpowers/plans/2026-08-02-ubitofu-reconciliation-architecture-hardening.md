# ubitofu Reconciliation Architecture Hardening Implementation Plan

Status: superseded by `2026-08-03-ubitofu-0.10-single-cutover.md`. Retained as
review history. Do not execute this plan.

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> superpowers:subagent-driven-development (recommended) or
> superpowers:executing-plans to implement this plan task-by-task. Steps use
> checkbox (`- [ ]`) syntax for tracking.

## Goal

Make generate/reconcile/verify fail closed, calculate reconciliation through
immutable resource decisions, and commit a complete candidate worktree through
a tested recovery protocol.

## Architecture

Controller and OpenTofu adapters produce deep-frozen boundary models. A pure
planner emits one `ResourceDecision` per observation. Rendering produces every
candidate file in memory. Two blocking spikes prove faithful offline OpenTofu
validation and exact HCL source spans before those features enter the pipeline. See
`docs/superpowers/specs/2026-08-02-ubitofu-reconciliation-architecture-design.md`.

## Tech stack

Python 3.11+, frozen dataclasses, enums, pathlib, fcntl, hashlib, ssl, httpx,
OpenTofu CLI, python-hcl2/tree-sitter-hcl feasibility candidates, pytest,
Hypothesis, Ruff, strict mypy, and mutmut.

## Global constraints

- Preserve the plan-only boundary. Do not add controller writes, `tofu apply`,
  `tofu import`, or state mutation.
- Preserve report terms and outcome codes except where a task names an approved
  breaking change. Exit 1 is an operational error. 10/11/12 and
  forbidden-create are reconciliation outcomes.
- Every new source file starts with the repository SPDX/copyright header.
- Use `python3` in local commands. Production code passes `mypy --strict` and
  the repository's Ruff rules.
- Run local commands inside an isolated project virtual environment initialized
  with `python3 -m venv .venv` and `pip install -e ".[dev]"`, matching the CI
  dependency set. The command blocks below assume that environment is active.
- Write the failing test first, confirm the expected failure, implement the
  smallest change, then run focused and task-boundary verification.
- Never persist plan binaries, generated stubs, credentials, raw provider
  stderr, transaction contents, or provider data outside mode-0700 `.ubitofu/`
  scratch. Add `.ubitofu/` to `.gitignore` before creating it.
- Commit subjects use kernel style: lowercase imperative with a subsystem
  prefix. Do not add AI attribution trailers.
- Tasks 5 and 7 are blocking feasibility gates. If either fails its stated
  contract, stop and revise the design. Do not implement a best-effort version
  under the same safety claim.

## Baseline

- [ ] Record branch, HEAD, status, and whether the ignored architecture files
  will remain local or be deliberately force-added later:

  ```bash
  git status --short --branch
  git rev-parse HEAD
  git check-ignore -v \
    docs/superpowers/specs/2026-08-02-ubitofu-reconciliation-architecture-design.md
  ```

- [ ] Run the current non-controller gates:

  ```bash
  python3 -m pytest -m "not controller" --cov=ubitofu --cov-branch
  python3 -m ruff check src tests
  python3 -m mypy src/ubitofu
  ```

  Expected: all green. Record environmental failures separately and do not
  weaken product tests around a broken local OpenTofu or Docker setup.

---

### Task 1: Lock the workdir, contain artifacts, and reject every failed plan

**Files:**

- Modify: `.gitignore`
- Create: `src/ubitofu/workdir_lock.py`
- Modify: `src/ubitofu/tofu_runner.py`
- Modify: `src/ubitofu/pipeline.py`
- Create: `tests/test_workdir_lock.py`
- Modify: `tests/test_tofu_runner.py`
- Modify: `tests/test_pipeline.py`
- Modify: `tests/test_reconcile.py`

**Interfaces:**

```python
@contextmanager
def workdir_lock(workdir: Path) -> Iterator[None]:
    """Hold a POSIX kernel lock from snapshot acquisition through cleanup."""

@dataclass(frozen=True)
class TofuExecutionError(TofuError):
    command_kind: str
    exit_code: int
    safe_reason: str

@contextmanager
def _artifact_dir(workdir: Path) -> Iterator[Path]:
    """Yield one mode-0700 child and remove that exact child in finally."""
```

- [ ] Add lock tests using a second process. Assert mutual exclusion, release on
  normal/exceptional exit, and rejection of a symlinked `.ubitofu`. Use
  `fcntl.flock`. A PID written for diagnostics is never used to infer staleness.

- [ ] Add artifact tests covering plan success, exit 2, exit 1 with a non-empty
  generated stub, JSON-show failure, `errored: true`, and a caller exception.
  Assert that only exit 0/2 plus `errored: false` returns usable data and that no
  fixed or scratch plan/stub remains. Assert `show`, `validate`, and schema/state
  inspection require exit 0 rather than inheriting plan's exit-2 allowance.

- [ ] Add a process-kill test that terminates a worker after its plan/stub exists.
  On the next invocation, acquire the workdir lock, safely reclaim every orphaned
  direct child under `.ubitofu/tmp`, and then continue. Reject symlinked or
  malformed residue rather than following or recursively trusting it.

- [ ] Add diagnostics tests whose fake stderr contains an API key, password,
  secret value, URL userinfo, ANSI escapes, 100 KiB payload, and HCL excerpt.
  Assert normal exceptions expose only command kind, status, and an allowlisted
  reason. Do not return raw stderr by default.

- [ ] Add `.ubitofu/` to `.gitignore`. Implement exact-child cleanup with
  restrictive permissions. Never remove the workdir, `.ubitofu`, a glob, a
  symlink target, or an unresolved path.

- [ ] Move generate/reconcile/verify plan ownership into the artifact context.
  Remove `tf.plan`, `verify.plan`, generated-stub filenames, and ad hoc unlinks.
  Acquire the workdir lock before any snapshot and hold it through final cleanup.

- [ ] Characterize the provider-invalid `-generate-config-out` case with the
  pinned real OpenTofu/provider. A failed run may expose partial HCL only as a
  local diagnostic. It cannot feed rendering or destination writes. If product
  behavior requires partial adoption, design a separate review-only mode before
  changing this rule.

- [ ] Run:

  ```bash
  python3 -m pytest tests/test_workdir_lock.py tests/test_tofu_runner.py \
    tests/test_pipeline.py tests/test_reconcile.py -q
  python3 -m ruff check src/ubitofu tests/test_workdir_lock.py tests/test_tofu_runner.py
  python3 -m mypy src/ubitofu
  ```

- [ ] Commit:

  ```bash
  git add .gitignore src/ubitofu/workdir_lock.py src/ubitofu/tofu_runner.py \
    src/ubitofu/pipeline.py tests/test_workdir_lock.py tests/test_tofu_runner.py \
    tests/test_pipeline.py tests/test_reconcile.py
  git commit -m "tofu: reject failed plans and contain private artifacts"
  ```

---

### Task 2: Ship verified TLS and response policy as a breaking change

**Files:**

- Modify: `src/ubitofu/config.py`
- Modify: `src/ubitofu/controller.py`
- Modify: `src/ubitofu/coverage.py`
- Modify: `src/ubitofu/cli.py`
- Modify: `tests/fixtures/config.toml`
- Modify: `tests/test_config.py`
- Modify: `tests/test_controller.py`
- Modify: `tests/test_coverage.py`
- Modify: `tests/test_cli.py`
- Modify: `README.md`
- Modify: `CHANGELOG.md`

**Interfaces:**

- `Config.verify_tls: bool = True`
- `Config.ca_bundle: str = ""`
- `Controller.verify: bool | ssl.SSLContext = True`
- `ControllerResponseError(endpoint, status, safe_reason)`
- an endpoint/dialect-specific absence-policy table

- [ ] Add failing tests for verified default, explicit insecure mode, custom CA,
  missing CA, and contradictory `verify_tls = false` plus `ca_bundle`.
  Construct custom trust with `ssl.create_default_context(cafile=...)`. Do not
  pass a CA path directly to HTTPX's deprecated string `verify=` form.

- [ ] Add `httpx.MockTransport` tests for 401, 403, expected/unexpected 404 and
  405, 429 with numeric and HTTP-date `Retry-After`, 500, malformed JSON, and
  malformed collection envelopes. Only the endpoint/dialect table can declare
  absence. Retry idempotent GETs only, with attempt and total-time limits.

- [ ] Add safe response diagnostics. Never echo bodies, auth headers, or URL
  userinfo.

- [ ] Implement the secure default and response policy. This task lands only in
  a declared breaking release. Update README and CHANGELOG in the same commit so
  self-signed-controller operators see `ca_bundle` and explicit
  `verify_tls = false` migration paths.

- [ ] Run:

  ```bash
  python3 -m pytest tests/test_config.py tests/test_controller.py \
    tests/test_coverage.py tests/test_cli.py -q
  python3 -m ruff check src/ubitofu tests/test_config.py tests/test_controller.py \
    tests/test_coverage.py tests/test_cli.py
  python3 -m mypy src/ubitofu
  ```

- [ ] Commit:

  ```bash
  git add src/ubitofu/config.py src/ubitofu/controller.py src/ubitofu/coverage.py \
    src/ubitofu/cli.py tests/fixtures/config.toml tests/test_config.py \
    tests/test_controller.py tests/test_coverage.py tests/test_cli.py \
    README.md CHANGELOG.md
  git commit -m "controller: verify TLS and classify responses precisely"
  ```

---

### Task 3: Model the exact OpenTofu JSON subset

**Files:**

- Create: `src/ubitofu/reconcile_model.py`
- Create: `src/ubitofu/tofu_json.py`
- Create: `tests/test_tofu_json.py`
- Modify: `src/ubitofu/tofu_runner.py`
- Modify: `src/ubitofu/pipeline.py`
- Modify: `src/ubitofu/coverage.py`
- Modify: `tests/test_tofu_runner.py`
- Modify: `tests/test_pipeline.py`
- Modify: `tests/test_coverage.py`

**Interfaces:**

```python
@dataclass(frozen=True)
class OpenTofuAddress:
    absolute: str
    module: str | None
    mode: Literal["managed", "data"]
    resource_type: str
    name: str
    index: str | int | None
    deposed: str | None

class ActionVector(Enum):
    NOOP = ("no-op",)
    CREATE = ("create",)
    READ = ("read",)
    UPDATE = ("update",)
    DELETE_CREATE = ("delete", "create")
    CREATE_DELETE = ("create", "delete")
    DELETE = ("delete",)
    FORGET = ("forget",)

@dataclass(frozen=True)
class ResourceChange:
    address: OpenTofuAddress
    action: ActionVector
    before: FrozenObject | None
    after: FrozenObject | None
    after_unknown: FrozenObject

def parse_plan_document(value: object) -> PlanDocument:
    """Validate supported format major, errored flag, and used fields."""
```

- [ ] Add table-driven tests for every legal action vector and its nullable
  before/after shape, full root/module/index/data/deposed addresses, `forget`,
  both replacement orders, malformed actions, malformed strict numeric format
  versions, supported unknown minors, future majors, missing fields, and
  `errored: true`.

- [ ] Deep-freeze every retained mapping/list recursively. A frozen dataclass
  containing a mutable nested dict does not pass the test. Ignore unknown fields
  for minor-version compatibility.

- [ ] Mark only root, managed, unindexed, current resources as editable.
  Normalize every other legal address into an explicit unsupported attention
  observation. Never reconstruct identity as `type.name` or silently merge
  instances.

- [ ] Validate only the provider-schema and state fields the product reads, but
  apply the same deep-freeze and field-path diagnostics. Migrate consumers
  without changing supported fixture reports or output bytes.

- [ ] Run:

  ```bash
  python3 -m pytest tests/test_tofu_json.py tests/test_tofu_runner.py \
    tests/test_pipeline.py tests/test_coverage.py -q
  python3 -m ruff check src/ubitofu/tofu_json.py src/ubitofu/reconcile_model.py \
    tests/test_tofu_json.py
  python3 -m mypy src/ubitofu
  ```

- [ ] Commit:

  ```bash
  git add src/ubitofu/reconcile_model.py src/ubitofu/tofu_json.py \
    src/ubitofu/tofu_runner.py src/ubitofu/pipeline.py src/ubitofu/coverage.py \
    tests/test_tofu_json.py tests/test_tofu_runner.py tests/test_pipeline.py \
    tests/test_coverage.py
  git commit -m "tofu: normalize the supported JSON contract"
  ```

---

### Task 4: Extract decisions and render every candidate before writing

**Files:**

- Modify: `src/ubitofu/reconcile_model.py`
- Create: `src/ubitofu/reconcile_planner.py`
- Create: `src/ubitofu/reconcile_renderer.py`
- Create: `tests/test_reconcile_planner.py`
- Create: `tests/test_reconcile_renderer.py`
- Modify: `src/ubitofu/pipeline.py`
- Modify: `tests/test_reconcile.py`
- Modify: `tests/test_properties.py`

**Interfaces:**

```python
@dataclass(frozen=True)
class ResourceDecision:
    address: OpenTofuAddress
    disposition: Disposition
    reason_code: ReasonCode
    edits: tuple[EditIntent, ...]

@dataclass(frozen=True)
class ReconcilePlan:
    decisions: tuple[ResourceDecision, ...]
    global_edits: tuple[EditIntent, ...]

def build_reconcile_plan(snapshot: ReconcileSnapshot) -> ReconcilePlan:
    """Map one normalized observation to one deterministic decision."""

def render_candidates(plan: ReconcilePlan, sources: SourceSnapshot) -> tuple[ProposedFile, ...]:
    """Return all destination contents without touching the filesystem."""
```

- [ ] Add characterization tests for no-op, scalar, complex, removed, pending,
  orphaned, codified, appended, forbidden, secret-variable, dangling-reference,
  coverage, and combined outcomes. Capture report, exit code, and output bytes.

- [ ] Add a decision table over exact action vector × committed presence × live
  identity × state identity × editable address. Every supported observation
  produces exactly one `ResourceDecision`. Impossible combinations raise a
  typed error.

- [ ] Add Hypothesis properties for determinism, input-order independence,
  single disposition, forbidden-not-pending, deep immutability, and absence of
  check/wet mode from planner inputs.

- [ ] Derive report categories and exit code from decisions. Do not store
  parallel `merged`/`removed`/`attention` lists or a stored exit code that can
  contradict decisions.

- [ ] Render all persistent outputs into `ProposedFile` values before the first
  write: operator edits, generated/import files, variable declarations,
  creations, deletions, stale bulk outputs, and `COVERAGE.md`. Preserve `lstat`
  identity, mode, size, mtime, and content hash in the source snapshot.

- [ ] Keep the existing write helpers only as one final compatibility commit
  call. Add an injected late render failure and assert zero destination writes.
  Both check and wet runs build equal plans and candidate bytes.

- [ ] Run:

  ```bash
  python3 -m pytest tests/test_reconcile_planner.py tests/test_reconcile_renderer.py \
    tests/test_reconcile.py tests/test_properties.py tests/test_pipeline.py -q
  python3 -m ruff check src/ubitofu/reconcile_model.py \
    src/ubitofu/reconcile_planner.py src/ubitofu/reconcile_renderer.py \
    tests/test_reconcile_planner.py tests/test_reconcile_renderer.py
  python3 -m mypy src/ubitofu
  ```

- [ ] Commit:

  ```bash
  git add src/ubitofu/reconcile_model.py src/ubitofu/reconcile_planner.py \
    src/ubitofu/reconcile_renderer.py src/ubitofu/pipeline.py \
    tests/test_reconcile_planner.py tests/test_reconcile_renderer.py \
    tests/test_reconcile.py tests/test_properties.py tests/test_pipeline.py
  git commit -m "reconcile: derive outputs from immutable decisions"
  ```

---

### Task 5: Prove complete offline staged validation

#### Blocking gate

Do not begin Task 6 until this passes on macOS and Linux.

Proof result, 2026-08-02: the bounded contract passes on macOS 15.7.5 arm64
with OpenTofu 1.12.0. It enforces network denial with `sandbox-exec`, filters
the staged dependency closure, rejects undeclared filesystem reads, and leaves
the destination unchanged. Linux is unsupported and unrun, so this gate remains
open. See `docs/staged-validation-contract.md`.

**Files:**

- Create: `src/ubitofu/staged_validation.py`
- Create: `tests/test_staged_validation.py`
- Create: `docs/staged-validation-contract.md`

**Module manifest:**

- active `.tf`, `.tofu`, `.tf.json`, and `.tofu.json` files with OpenTofu
  replacement/override precedence.
- `.terraform.lock.hcl`.
- Resolved local-module sources, including sources outside the workdir.
- Already-installed provider and registry-module data needed by `validate`.
- no state, plan, variable-value, credential, `.git`, or unrelated files.

- [ ] Write real-OpenTofu fixtures for all four configuration suffixes,
  precedence pairs, overrides, a lockfile, nested local modules inside/outside
  the root, and an already-installed registry provider/module.

- [ ] Test the candidate root with a private `TF_DATA_DIR` and
  `tofu init -backend=false` only if initialization is required. Force registry
  access to fail and use trap backend/provider endpoints. Assert validation
  performs no network download, backend access, provider API request, or write
  to the destination module. Missing cached dependencies must say
  `initialize first` rather than downloading silently.

- [ ] Hash and `lstat` the destination before/after every test. Prove validation
  sees the candidate change and the same effective module OpenTofu will see in
  the real workdir.

- [ ] Document the exact OpenTofu versions, environment variables, copied data,
  platform results, and unsupported cases. If the contract fails, stop here and
  revise the design to omit mandatory pre-commit `tofu validate` or use a
  separately approved strategy.

- [ ] Run on each platform:

  ```bash
  python3 -m pytest tests/test_staged_validation.py -q
  python3 -m ruff check src/ubitofu/staged_validation.py tests/test_staged_validation.py
  python3 -m mypy src/ubitofu
  ```

- [ ] Commit only after both platform results are recorded:

  ```bash
  git add src/ubitofu/staged_validation.py tests/test_staged_validation.py \
    docs/staged-validation-contract.md
  git commit -m "tofu: prove offline staged validation contract"
  ```

---

### Task 6: Commit candidates with durable recovery

**Files:**

- Create: `src/ubitofu/file_transaction.py`
- Create: `tests/test_file_transaction.py`
- Create: `tests/helpers/transaction_crash_worker.py`
- Modify: `src/ubitofu/pipeline.py`
- Modify: `tests/test_pipeline.py`
- Modify: `tests/test_reconcile.py`
- Modify: `tests/test_integration.py`

**Protocol:**

- immutable, versioned manifest with relative paths, original/candidate/backup
  hashes, original `lstat` identity/mode, and candidate mode. Write it through a
  temporary file, file fsync, atomic rename, and transaction-directory fsync
  before destination mutation.
- Fsynced candidates and complete backups before destination mutation.
- a small phase enum whose marker is temp-written, fsynced, atomically renamed,
  then followed by transaction-directory fsync.
- Deterministic, no-follow destination operations and parent-directory fsync.
- idempotent startup recovery or quarantine on ambiguity.

- [ ] Add unit tests for replace/create/delete, multi-file success, duplicate or
  escaping paths, symlinked `.ubitofu`, symlink in every destination component,
  metadata-only concurrent changes, atomic editor replacement, restrictive new
  file mode, existing mode preservation, and a non-cooperating writer.

- [ ] Add process-kill tests before and after every durable boundary. Cover
  corrupt/truncated manifests and phase markers, a missing candidate/backup,
  multiple residual transactions, successful recovery, rollback failure, and
  external changes during recovery.

- [ ] Define the guarantee in tests: success gives complete new. Failed commit
  plus successful rollback gives complete old. Rollback failure quarantines the
  transaction, preserves backups, blocks all future mutation, and reports manual
  recovery paths. Never claim old-or-new after rollback failure.

- [ ] Integrate Task 5 validation and the transaction into generate/reconcile.
  `--check` stages and validates identical candidates but never starts commit.
  Verify uses only the private artifact manager.

- [ ] Run:

  ```bash
  python3 -m pytest tests/test_file_transaction.py tests/test_pipeline.py \
    tests/test_reconcile.py tests/test_integration.py -q
  python3 -m ruff check src/ubitofu/file_transaction.py \
    tests/test_file_transaction.py tests/helpers/transaction_crash_worker.py
  python3 -m mypy src/ubitofu
  ```

- [ ] Commit:

  ```bash
  git add src/ubitofu/file_transaction.py src/ubitofu/pipeline.py \
    tests/test_file_transaction.py tests/helpers/transaction_crash_worker.py \
    tests/test_pipeline.py tests/test_reconcile.py tests/test_integration.py
  git commit -m "pipeline: commit candidates with durable recovery"
  ```

---

### Task 7: Select and roll out a structural span engine

#### Blocking gate

Compare candidates first. Roll out only after one passes the same corpus and
Task 6 protects all destination edits.

Proof result, 2026-08-02: tree-sitter-hcl 1.2.0 with tree-sitter 0.26.0 is the
selected engine. It matched all 53 literal expected spans. python-hcl2 missed a
qualified reference inside heredoc interpolation. This result selects the
engine only. The production index, dependency change, and rollout below remain
unimplemented and must wait for Task 6. See `docs/hcl-parser-decision.md`.

**Files:**

- Create: `src/ubitofu/hcl_index.py`
- Create: `tests/test_hcl_index.py`
- Create: `tests/fixtures/hcl/structural_edge_cases.tf`
- Create: `docs/hcl-parser-decision.md`
- Modify: `src/ubitofu/hcl_surgeon.py`
- Modify: `src/ubitofu/pipeline.py`
- Modify: `tests/test_hcl_surgeon.py`
- Modify: `tests/test_properties.py`

#### Corpus contract

Exact original-byte spans for native `.tf`/`.tofu`. Strict JSON discovery for
`.tf.json`/`.tofu.json`. CRLF, LF, mixed newlines, BOM policy,
Unicode before/inside spans, no final newline, heredocs, interpolation, comments,
nested blocks, duplicate-looking text, imports, variables, and references.

- [ ] Write the corpus and expected byte spans before adapters. Add the existing
  reconcile fixtures and the largest available real module.

- [ ] Implement disposable adapters for:

  1. python-hcl2 `parses_to_tree()` with raw-byte input, parser-only LF
     normalization, and a total normalized-character-to-original-byte map.
  2. tree-sitter-hcl byte ranges with hard rejection of every `ERROR` or
     `MISSING` node.
  3. a small Go `hclsyntax` helper only if both Python options fail.

- [ ] Compare correctness, fail-closed behavior, install footprint, and measured
  real-module latency. Record the evidence and selection in
  `docs/hcl-parser-decision.md`. Stop and revise the architecture if no candidate
  passes.

- [ ] Route resource spans, committed addresses, imports, variable declarations,
  and expression-reference discovery through the selected engine. Existing
  operator-owned JSON resources are discoverable but edit-required drift is an
  attention result. ubitofu-owned JSON may be regenerated transactionally.

- [ ] Preserve compatibility APIs in `hcl_surgeon.py`. Verify expected old bytes,
  reject duplicate addresses and overlapping patches, and apply normalized patch
  sets from highest byte offset downward. Define the property precisely: a set
  of disjoint patches yields one canonical result regardless of input ordering.

- [ ] Run:

  ```bash
  python3 -m pytest tests/test_hcl_index.py tests/test_hcl_surgeon.py \
    tests/test_properties.py tests/test_reconcile.py -q
  python3 -m ruff check src/ubitofu/hcl_index.py src/ubitofu/hcl_surgeon.py \
    tests/test_hcl_index.py
  python3 -m mypy src/ubitofu
  ```

- [ ] Commit implementation and decision evidence together:

  ```bash
  git add src/ubitofu/hcl_index.py src/ubitofu/hcl_surgeon.py \
    src/ubitofu/pipeline.py tests/test_hcl_index.py tests/test_hcl_surgeon.py \
    tests/test_properties.py tests/fixtures/hcl/structural_edge_cases.tf \
    docs/hcl-parser-decision.md
  git commit -m "hcl: select and enforce structural source spans"
  ```

---

### Task 8: Mutation-gate the expanded safety core

**Files:**

- Modify: `pyproject.toml`
- Modify: `ci/mutation_gate.py`
- Modify: `.woodpecker/ci.yml`
- Modify: `tests/test_infra_deps.py`
- Create: `docs/testing.md`
- Modify: planner/JSON/transaction/index tests as survivors require

- [ ] Update infrastructure tests first. The complete PR scope retains
  `enumerator.py`, `import_emitter.py`, and the selected HCL editor, and adds
  `workdir_lock.py`, `tofu_json.py`, `reconcile_planner.py`,
  `reconcile_renderer.py`, `staged_validation.py`, and `file_transaction.py`.
  Keep the weekly sweep at least this broad.

- [ ] Add a non-mutating `check`/dry-run mode to `ci/mutation_gate.py`, or restore
  any temporary `pyproject.toml` rewrite in `finally`. Test that local execution
  leaves tracked files byte-identical.

- [ ] Measure each new module before enforcing zero survivors. Shard by module if
  needed. Kill meaningful survivors with behavior tests. Annotate only proven
  equivalent mutants with a line-level reason.

- [ ] Move `pipeline.py` out of mutation scope only after an objective checked
  threshold shows it is a thin composition shell. Keep branch coverage.

- [ ] Run the actual interface implemented above. If `check` mode was added:

  ```bash
  python3 ci/mutation_gate.py check
  ```

  Otherwise use the existing required mode with an explicit changed-file set and
  verify cleanup:

  ```bash
  CI_PIPELINE_FILES='["src/ubitofu/reconcile_planner.py"]' \
    python3 ci/mutation_gate.py pr
  git diff --exit-code -- pyproject.toml
  ```

- [ ] Commit:

  ```bash
  git add pyproject.toml ci/mutation_gate.py .woodpecker/ci.yml \
    tests/test_infra_deps.py tests/test_workdir_lock.py tests/test_tofu_json.py \
    tests/test_reconcile_planner.py tests/test_reconcile_renderer.py \
    tests/test_staged_validation.py tests/test_file_transaction.py \
    tests/test_hcl_index.py docs/testing.md
  git commit -m "ci: mutation-test the reconciliation safety core"
  ```

---

### Task 9: Run release-readiness verification

**Files:**

- Modify as evidence requires: `README.md`, `CHANGELOG.md`, this design, and
  this plan

- [ ] Run the complete default gate from a clean process:

  ```bash
  python3 -m pytest -m "not controller" --cov=ubitofu --cov-branch
  python3 -m ruff check src tests
  python3 -m mypy src/ubitofu
  git diff --check
  ```

- [ ] Run real-OpenTofu, process-kill, staged-validation, and parser corpus tests
  on macOS and Linux. Inspect the repository and fixture workdirs for plan files,
  transaction residue, raw stderr, credentials, and unexpected mode changes.

- [ ] Run selected live-controller scenarios when Docker/controller URLs exist:

  ```bash
  python3 -m pytest -m "controller and not uos" \
    tests/controllertest/test_scenarios_reconcile.py -q
  ```

  If unavailable, record the unrun release gate. A skip is not live verification.

- [ ] Exercise a self-signed controller through explicit insecure mode, a custom
  CA endpoint, and an untrusted default. Confirm logs contain no credentials.

- [ ] Compare baseline and final report text, outcome code, and output bytes for
  every supported fixture. Review tracked, ignored, and staged changes for
  generated data or secrets.

- [ ] Request an independent code review focused on model completeness, offline
  validation, kill-point recovery, parser spans, metadata preservation, and
  compatibility. Resolve critical/important findings before release.

- [ ] If the architecture documents are intentionally tracked, force-add only
  the named ignored files and review the cached diff before committing:

  ```bash
  git add -f \
    docs/superpowers/specs/2026-08-02-ubitofu-reconciliation-architecture-design.md \
    docs/superpowers/specs/2026-08-02-ubitofu-reconciliation-architecture-design-review.md \
    docs/superpowers/plans/2026-08-02-ubitofu-reconciliation-architecture-hardening.md
  git diff --cached --check
  git diff --cached --stat
  git commit -m "docs: document reconciliation safety architecture"
  ```

## Completion criteria

Do not call this plan complete until both blocking gates pass, every design
acceptance criterion has a test or named live result, the complete default and
mutation gates are green, and no plan artifact or transaction residue remains.
A green unit suite alone is not production proof for TLS, OpenTofu staging,
crash recovery, or live controller compatibility.
