# ubitofu Public Interfaces Implementation Plan

Status: superseded by `2026-08-03-ubitofu-0.10-single-cutover.md`. Retained as
review history. Do not execute this phase independently.

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> superpowers:subagent-driven-development (recommended) or
> superpowers:executing-plans to implement this plan task-by-task. Steps use
> checkbox (`- [ ]`) syntax for tracking.

**Goal:** Expose the stable Unix command surface that generic local and CI
consumers can compose without importing ubitofu internals or duplicating UniFi
policy.

**Architecture:** Commands call the same validated snapshots, pure decisions,
and transaction core established in phases 1-4. A single outcome model renders
human output or a versioned JSON receipt. `check` inspects exactly a caller-
supplied saved plan and reports its digest; it never generates a replacement.
`inspect` owns provider/controller coverage interpretation. `health` owns UniFi
health normalization and comparison. The CLI knows nothing about Woodpecker,
GitHub, Git, state backup, apply, or notifications.

**Tech Stack:** Python 3.11+, argparse, frozen dataclasses, enums, strict JSON,
SHA-256, OpenTofu show JSON, existing controller adapter, pytest CLI/subprocess
tests, Ruff, strict mypy, mutmut.

## Global Constraints

- Phases 1-4 must be complete and released before consumers rely on this
  surface.
- Preserve the read-only-infrastructure promise. `check`, `inspect`, and health
  commands write no HCL, controller data, state, or plan file.
- Primary human/JSON output goes to stdout or the exact `--output` path.
  Operational diagnostics and deprecation warnings go to stderr.
- Every command supports `--format human|json` and `--output PATH|-` with the
  same parsing rules. Refuse binary destinations, symlinks, and accidental
  overwrite unless the command contract explicitly permits a new regular file.
- JSON receipts contain stable reason codes, safe addresses, counts, and input
  digests. They never contain HCL values, controller payloads, credentials,
  response bodies, provider stderr, URLs, or filesystem paths outside explicitly
  supplied safe relative paths.
- Human and JSON output derive from the same immutable outcome. Do not rerun or
  reclassify work during rendering.
- Keep `enumerate`, `verify`, and `reconcile --check` for one major-version
  cycle. They use the new core and print a deprecation warning, but their
  existing output and exit behavior remain compatible.
- Do not add a `ci` command, CI-vendor discovery, workflow templates, Git/PR
  commands, state backup, apply, or notification integrations.

---

### Task 1: Define one outcome and versioned receipt envelope

**Files:**

- Create: `src/ubitofu/outcomes.py`
- Create: `src/ubitofu/receipt.py`
- Create: `src/ubitofu/output.py`
- Create: `tests/test_outcomes.py`
- Create: `tests/test_receipt.py`
- Create: `tests/test_output.py`

**Interfaces:**

```python
class OutcomeKind(Enum):
    OK = "ok"
    CHANGED = "changed"
    ATTENTION = "attention"
    CHANGED_WITH_ATTENTION = "changed_with_attention"
    FORBIDDEN = "forbidden"

@dataclass(frozen=True)
class OutcomeItem:
    reason_code: str
    severity: Literal["info", "warning", "blocking"]
    address: str | None
    message: str

@dataclass(frozen=True)
class CommandOutcome:
    command: str
    kind: OutcomeKind
    exit_code: Literal[0, 10, 11, 12, 13]
    summary: str
    items: tuple[OutcomeItem, ...]
    inputs: tuple[tuple[str, str], ...]

@dataclass(frozen=True)
class ReceiptEnvelope:
    schema: Literal["dev.ubitofu.receipt"]
    version: Literal[1]
    outcome: CommandOutcome
    payload: FrozenObject | None

def receipt_json(
    outcome: CommandOutcome,
    *,
    payload: FrozenObject | None = None,
) -> bytes: ...
def render_human(outcome: CommandOutcome) -> str: ...
def emit_output(
    outcome: CommandOutcome,
    *,
    payload: FrozenObject | None,
    format: Literal["human", "json"],
    output: str,
    stdout: IO[str],
) -> None: ...
```

- [ ] Add tests that enforce the exit mapping 0/10/11/12/13, deterministic item
  and payload ordering, exact schema/version, canonical sorted-key JSON with one
  trailing newline, and byte-identical rerendering. Command-specific payloads
  are validated immutable values, never arbitrary dictionaries.

- [ ] Add redaction tests seeded with API keys, passwords, HCL values, URL
  userinfo, ANSI escapes, controller bodies, absolute private paths, and
  oversized messages. The outcome constructor must accept only already-safe
  fields and reject control characters or unbounded text.

- [ ] Add output tests for stdout, a new output file, `-`, pre-existing files,
  parent symlinks, destination symlinks, short writes, fsync failure, and
  operational exceptions. Receipt-file writes are single-file atomic via a
  same-directory temporary file and replace; they are not HCL transactions.

- [ ] Confirm red:

  ```bash
  python3 -m pytest tests/test_outcomes.py tests/test_receipt.py \
    tests/test_output.py -q
  ```

- [ ] Implement one constructor per command that maps domain decisions into
  `CommandOutcome`. Renderers may not inspect `ReconcilePlan`, controller data,
  or OpenTofu JSON directly.

- [ ] Verify:

  ```bash
  python3 -m pytest tests/test_outcomes.py tests/test_receipt.py \
    tests/test_output.py -q
  python3 -m ruff check src/ubitofu/outcomes.py src/ubitofu/receipt.py \
    src/ubitofu/output.py tests/test_outcomes.py tests/test_receipt.py \
    tests/test_output.py
  python3 -m mypy src/ubitofu
  ```

- [ ] Commit:

  ```bash
  git add src/ubitofu/outcomes.py src/ubitofu/receipt.py \
    src/ubitofu/output.py tests/test_outcomes.py tests/test_receipt.py \
    tests/test_output.py
  git commit -m "output: define stable human and JSON command outcomes"
  ```

---

### Task 2: Check exactly one caller-supplied saved plan

**Files:**

- Create: `src/ubitofu/plan_check.py`
- Modify: `src/ubitofu/tofu_runner.py`
- Modify: `src/ubitofu/cli.py`
- Create: `tests/test_plan_check.py`
- Modify: `tests/test_tofu_runner.py`
- Modify: `tests/test_cli.py`
- Modify: `tests/test_integration.py`

**Interfaces:**

```python
@dataclass(frozen=True)
class SavedPlanIdentity:
    sha256: str
    size: int
    device: int
    inode: int

@dataclass(frozen=True)
class PlanSafetyDecision:
    reason_code: str
    disposition: Literal["allow", "block", "attention"]
    address: OpenTofuAddress | None
    message: str

@dataclass(frozen=True)
class PlanCheckResult:
    plan: SavedPlanIdentity
    decisions: tuple[PlanSafetyDecision, ...]

def check_saved_plan(
    *,
    cfg: Config,
    plan_path: Path,
    controller: Controller,
    runner: TofuRunner,
) -> CommandOutcome: ...
```

- [ ] Add parser tests for:

  ```text
  ubitofu check --config ubitofu.toml --plan tfplan.out
  ubitofu check --config ubitofu.toml --plan tfplan.out --format json
  ubitofu check --config ubitofu.toml --plan tfplan.out --format json --output receipt.json
  ```

  `--plan` is required and no `apply`, `init`, backend, Git, or CI option exists.

- [ ] Add a recording-runner test proving `check` calls `tofu show -json` for
  the supplied path and never calls `tofu plan`. Exit 0 from `tofu show` is
  required. The saved plan bytes must be unchanged before/after.

- [ ] Add no-follow identity tests: missing/empty plan, symlink, directory,
  non-regular file, atomic replacement during show, content mutation during
  show, inode reuse attempt, and digest mismatch. Hash the opened file before
  show and recheck lstat plus hash after show; any change is `StaleSnapshotError`.

- [ ] Add the safety matrix: no-op, ordinary code update, independent compatible
  UI/HCL changes, equal concurrent result, uncaptured live drift, same-field
  conflict, delete-versus-modify, unsupported address, provider unknown,
  malformed/errored plan, high-risk firewall/VPN/network changes as explicit
  warnings, and forbidden `unifi_device` create.

- [ ] Confirm red:

  ```bash
  python3 -m pytest tests/test_plan_check.py tests/test_tofu_runner.py \
    tests/test_cli.py tests/test_integration.py -q
  ```

- [ ] Implement `check` by collecting the current immutable reconciliation
  snapshot, parsing the supplied saved plan, and deriving safety decisions from
  the same semantic rules as reconcile. Do not modify HCL or produce candidates.

- [ ] Put `plan_sha256` and `plan_size` in receipt inputs. Human output names the
  digest. Document that authorization applies only to those exact bytes.

- [ ] Verify:

  ```bash
  python3 -m pytest tests/test_plan_check.py tests/test_tofu_runner.py \
    tests/test_cli.py tests/test_integration.py -q
  python3 -m ruff check src/ubitofu/plan_check.py src/ubitofu/cli.py \
    tests/test_plan_check.py
  python3 -m mypy src/ubitofu
  ```

- [ ] Commit:

  ```bash
  git add src/ubitofu/plan_check.py src/ubitofu/tofu_runner.py \
    src/ubitofu/cli.py tests/test_plan_check.py tests/test_tofu_runner.py \
    tests/test_cli.py tests/test_integration.py
  git commit -m "check: authorize the exact supplied OpenTofu plan"
  ```

---

### Task 3: Expose provider and controller coverage through inspect

**Files:**

- Create: `src/ubitofu/inspect.py`
- Modify: `src/ubitofu/coverage.py`
- Modify: `src/ubitofu/enumerator.py`
- Modify: `src/ubitofu/cli.py`
- Create: `tests/test_inspect.py`
- Modify: `tests/test_coverage.py`
- Modify: `tests/test_enumerator.py`
- Modify: `tests/test_cli.py`

**Interfaces:**

```python
@dataclass(frozen=True)
class CoverageFinding:
    reason_code: str
    endpoint_id: str
    object_kind: str
    count: int
    status: Literal["covered", "gap", "attention"]

@dataclass(frozen=True)
class Inspection:
    findings: tuple[CoverageFinding, ...]

def inspect_coverage(
    *,
    cfg: Config,
    controller: Controller,
    runner: TofuRunner,
) -> CommandOutcome: ...
```

- [ ] Add CLI tests for `ubitofu inspect --config ...` in human/JSON/file modes.
  Assert it performs no plan and no writes.

- [ ] Convert current coverage fixtures into exact ordered findings. Distinguish
  policy-listed absent endpoints, supported empty collections, provider schema
  gaps, unmapped controller objects, auth/rate-limit failures, malformed
  responses, and operational endpoint failures.

- [ ] Confirm red:

  ```bash
  python3 -m pytest tests/test_inspect.py tests/test_coverage.py \
    tests/test_enumerator.py tests/test_cli.py -q
  ```

- [ ] Move coverage interpretation out of report strings into `Inspection`.
  `format_coverage()` becomes a compatibility renderer over findings. Use safe
  endpoint identifiers from the controller policy, never raw URLs.

- [ ] Keep `enumerate` behavior for the compatibility period because it also
  emits imports. Make it consume the same inspection result rather than
  independently classifying coverage.

- [ ] Verify:

  ```bash
  python3 -m pytest tests/test_inspect.py tests/test_coverage.py \
    tests/test_enumerator.py tests/test_cli.py -q
  python3 -m ruff check src/ubitofu/inspect.py tests/test_inspect.py
  python3 -m mypy src/ubitofu
  ```

- [ ] Commit:

  ```bash
  git add src/ubitofu/inspect.py src/ubitofu/coverage.py \
    src/ubitofu/enumerator.py src/ubitofu/cli.py tests/test_inspect.py \
    tests/test_coverage.py tests/test_enumerator.py tests/test_cli.py
  git commit -m "inspect: expose normalized controller coverage findings"
  ```

---

### Task 4: Add normalized health snapshot and comparison

**Files:**

- Create: `src/ubitofu/health.py`
- Modify: `src/ubitofu/controller.py`
- Modify: `src/ubitofu/cli.py`
- Create: `tests/test_health.py`
- Modify: `tests/test_controller.py`
- Modify: `tests/test_cli.py`
- Create: `tests/fixtures/health/before.json`
- Create: `tests/fixtures/health/after.json`

**Interfaces:**

```python
class HealthRank(IntEnum):
    OK = 0
    UNKNOWN = 1
    WARNING = 2
    ERROR = 3

@dataclass(frozen=True, order=True)
class SubsystemHealth:
    subsystem: str
    status: str
    rank: HealthRank

@dataclass(frozen=True)
class HealthSnapshot:
    subsystems: tuple[SubsystemHealth, ...]

@dataclass(frozen=True)
class HealthDelta:
    subsystem: str
    before: SubsystemHealth
    after: SubsystemHealth
    degraded: bool

def capture_health(controller: Controller) -> HealthSnapshot: ...
def compare_health(before: HealthSnapshot, after: HealthSnapshot) -> tuple[HealthDelta, ...]: ...
```

- [ ] Add parser tests for `health snapshot` and `health compare --before FILE`
  with human/JSON/file output. Compare fetches the current snapshot itself.
  `--before` accepts a version-1 `dev.ubitofu.receipt` whose command is
  `health.snapshot` and whose validated payload contains the subsystem snapshot.

- [ ] Add controller-envelope tests for empty, duplicate, missing subsystem,
  missing status, non-string values, unknown future status, malformed JSON,
  authentication failure, rate limit, and unreachable controller.

- [ ] Add comparison tests for ok→warning/error, warning→error, recovery,
  unchanged baseline errors, new/missing subsystems, unknown status, input
  schema/version errors, duplicate entries, and deterministic output. Only rank
  worsening is degraded; existing baseline errors do not fail by themselves.

- [ ] Confirm red:

  ```bash
  python3 -m pytest tests/test_health.py tests/test_controller.py \
    tests/test_cli.py -q
  ```

- [ ] Implement health collection through the validated controller adapter.
  Normalize known statuses with the rank table. Preserve an unknown safe token
  as status with `UNKNOWN` rank and an attention item; do not silently treat it
  as `ok`.

- [ ] Map no degradation to exit 0 and any degradation/unknown-contract
  attention to exit 11. Health outputs include subsystem/status transitions but
  no controller URL, credentials, raw response fields, or unrelated device data.

- [ ] Verify:

  ```bash
  python3 -m pytest tests/test_health.py tests/test_controller.py \
    tests/test_cli.py -q
  python3 -m ruff check src/ubitofu/health.py tests/test_health.py
  python3 -m mypy src/ubitofu
  ```

- [ ] Commit:

  ```bash
  git add src/ubitofu/health.py src/ubitofu/controller.py \
    src/ubitofu/cli.py tests/test_health.py tests/test_controller.py \
    tests/test_cli.py tests/fixtures/health/before.json \
    tests/fixtures/health/after.json
  git commit -m "health: compare normalized controller subsystem status"
  ```

---

### Task 5: Unify CLI output and preserve compatibility

**Files:**

- Modify: `src/ubitofu/cli.py`
- Modify: `src/ubitofu/pipeline.py`
- Modify: `src/ubitofu/reporter.py`
- Create: `src/ubitofu/deprecations.py`
- Modify: `tests/test_cli.py`
- Modify: `tests/test_pipeline.py`
- Modify: `tests/test_reporter.py`
- Create: `tests/test_compatibility.py`
- Modify: `README.md`
- Modify: `CHANGELOG.md`

- [ ] Add a complete parser matrix for `generate`, `reconcile`, `check`,
  `inspect`, `health snapshot`, `health compare`, and compatibility commands.
  Assert consistent `--config`, `--format`, and `--output` semantics and no
  mutation/deployment options.

- [ ] Add golden old/new compatibility fixtures for `enumerate`, `verify`, and
  `reconcile --check`: report bytes, stdout/stderr split, exits, no writes, and
  one bounded deprecation warning. The aliases expire at the next major version,
  recorded in CHANGELOG.

- [ ] Confirm red:

  ```bash
  python3 -m pytest tests/test_cli.py tests/test_pipeline.py \
    tests/test_reporter.py tests/test_compatibility.py -q
  ```

- [ ] Make every command return `CommandOutcome` to `main()`. Centralize output
  emission and exception mapping there. Remove command-specific direct printing
  except compatibility adapters that are locked by golden tests.

- [ ] Update README with both ordinary workflows:

  ```text
  ubitofu reconcile --config ubitofu.toml
  git diff

  tofu init
  tofu plan -out=tfplan.out
  ubitofu check --config ubitofu.toml --plan tfplan.out --format json --output receipt.json
  # external system verifies receipt plan_sha256 against tfplan.out
  # external state backup
  tofu apply tfplan.out
  ```

  State explicitly that ubitofu does not apply, back up state, commit, push,
  open PRs, notify, or own CI scheduling.

- [ ] Document the digest binding: after `check`, the external system must
  recompute SHA-256 immediately before apply and refuse if it differs from the
  receipt. Applying by saved-plan path alone does not prove byte identity across
  a non-cooperating replacement.

- [ ] Verify:

  ```bash
  python3 -m pytest tests/test_cli.py tests/test_pipeline.py \
    tests/test_reporter.py tests/test_compatibility.py -q
  python3 -m ruff check .
  python3 -m mypy src
  git diff --check
  ```

- [ ] Commit:

  ```bash
  git add src/ubitofu/cli.py src/ubitofu/pipeline.py \
    src/ubitofu/reporter.py src/ubitofu/deprecations.py tests/test_cli.py \
    tests/test_pipeline.py tests/test_reporter.py tests/test_compatibility.py \
    README.md CHANGELOG.md
  git commit -m "cli: expose the stable plan-only command surface"
  ```

---

### Task 6: Mutation-test and release-verify public contracts

**Files:**

- Modify: `pyproject.toml`
- Modify: `ci/mutation_gate.py`
- Modify: `.woodpecker/ci.yml`
- Modify: `.github/workflows/ci.yml`
- Modify: `tests/test_infra_deps.py`
- Modify: `docs/testing.md`

- [ ] Add `outcomes.py`, `receipt.py`, `plan_check.py`, `inspect.py`, and
  `health.py` to mutation scope and CI path filters. Kill meaningful survivors
  in exit mapping, digest comparison, decision severity, health rank, schema
  version, and redaction.

- [ ] Run:

  ```bash
  python3 -m pytest -m "not controller" --cov=ubitofu --cov-branch
  python3 -m ruff check .
  python3 -m mypy src
  python3 ci/mutation_gate.py check
  python3 -m build
  git diff --check
  ```

- [ ] Run CLI subprocess tests from the built wheel and selected controller
  scenarios for generate/reconcile/inspect/health. Confirm plan check leaves the
  supplied plan byte-identical and no command emits credentials or raw payloads.

- [ ] Publish a prerelease and run the phase-6 consumer dual-run against that
  artifact before declaring the public contracts stable.

- [ ] Request an adversarial review focused on exact-plan identity, time-of-
  check/time-of-apply wording, receipt redaction/versioning, stdout/stderr,
  health unknowns, compatibility expiry, and scope-boundary leakage.

- [ ] Commit:

  ```bash
  git add pyproject.toml ci/mutation_gate.py .woodpecker/ci.yml \
    .github/workflows/ci.yml tests/test_infra_deps.py docs/testing.md
  git commit -m "ci: gate stable plan-only automation contracts"
  ```

## Completion Gate

Phase 5 is complete only when `check` demonstrably never plans or writes,
receipts bind the exact supplied plan digest, human/JSON outputs derive from one
outcome, health and inspect own their semantic parsing, compatibility commands
match their golden contracts with deprecation warnings, built-wheel CLI and
selected controller scenarios pass, and public-contract mutations have no
unexplained survivors.
