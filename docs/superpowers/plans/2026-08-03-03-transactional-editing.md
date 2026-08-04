# ubitofu Transactional Editing Implementation Plan

Status: superseded by `2026-08-03-ubitofu-0.10-single-cutover.md`. Retained as
review history. Do not execute this phase independently.

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> superpowers:subagent-driven-development (recommended) or
> superpowers:executing-plans to implement this plan task-by-task. Steps use
> checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the phase-2 legacy write loop with validated staging and a
durable, recoverable multi-file commit protocol.

**Architecture:** A `ReconcilePlan` renders immutable `ProposedFile` values.
Production staged validation builds the same effective OpenTofu module in a
private tree and runs `tofu validate` with kernel-enforced network denial. A
small persisted transaction protocol prepares fsynced candidates and complete
backups, records every durable transition, checks source identities again, and
commits or rolls back. It is implemented directly, without a state-machine
framework.

**Tech Stack:** Python 3.11+, `pathlib`, `os.open`/`O_NOFOLLOW`, `lstat`/`fstat`,
SHA-256, JSON manifests, fsync, atomic rename/replace, `fcntl`, OpenTofu 1.12.x,
macOS `sandbox-exec`, Linux user/network namespaces, pytest subprocess workers,
Ruff, strict mypy, mutmut.

## Global Constraints

- Phases 1 and 2 must be complete. Hold their workdir lock from snapshot
  acquisition through validation, commit/check completion, and cleanup.
- Do not copy the proof harness wholesale into runtime. Extract the proved
  contract behind production-sized interfaces using new default-suite tests.
- Never run `tofu init` during staged validation. Missing cached prerequisites
  fail with `initialize first`.
- Never fall back to unsandboxed validation when kernel enforcement is missing
  or denied.
- Never hardlink source files into a tree that formatting or validation may
  write. Copy with no-follow checks.
- Never claim a POSIX multi-file atomic transaction. The precise guarantee is:
  success gives complete new; failed commit plus successful rollback gives
  complete old; ambiguity or rollback failure quarantines recoverable backups
  and blocks mutation.
- Every path in a manifest is normalized, relative, non-empty, non-escaping,
  and independently resolved below the workdir and transaction root.
- Reject symlinked `.ubitofu`, symlinks in any destination component,
  non-regular source files, duplicate destinations, and cross-device staging.
- `reconcile --check` builds and validates the exact same plan and candidates as
  wet mode but never creates a commit-intent marker or mutates a destination.

---

### Task 1: Promote the proved effective-module manifest

**Files:**

- Create: `src/ubitofu/module_manifest.py`
- Create: `tests/test_module_manifest.py`
- Reuse fixtures from: `proofs/test_staged_validation_proof.py`
- Reference: `tools/staged_validation_proof.py`
- Reference: `docs/staged-validation-contract.md`

**Interfaces:**

```python
@dataclass(frozen=True)
class ManifestFile:
    source: Path | None
    stage_relative_path: PurePosixPath
    candidate_bytes: bytes | None
    mode: int

@dataclass(frozen=True)
class EffectiveModuleManifest:
    root_files: tuple[ManifestFile, ...]
    local_module_files: tuple[ManifestFile, ...]
    registry_module_files: tuple[ManifestFile, ...]
    provider_files: tuple[ManifestFile, ...]
    lockfile: ManifestFile
    modules_metadata: ManifestFile | None

def build_effective_module_manifest(
    *,
    workdir: Path,
    candidates: tuple[ProposedFile, ...],
) -> EffectiveModuleManifest: ...
```

- [ ] Port proof expectations into normal unit tests for active `.tf`, `.tofu`,
  `.tf.json`, `.tofu.json`, same-stem `.tofu` precedence, override precedence,
  candidate shadowing, a filtered lockfile, relative local modules, registry
  module metadata, and exact provider packages.

- [ ] Add rejection tests for state, plan, variable-value, credential, `.git`,
  and unrelated files; absolute/escaping local modules; malformed provider
  components; duplicate module labels after override resolution; symlinks;
  raced file replacement; and every `file*` configuration function.

- [ ] Confirm red:

  ```bash
  python3 -m pytest tests/test_module_manifest.py -q
  ```

- [ ] Implement source reads with `lstat`, `os.open(..., O_NOFOLLOW)`, `fstat`,
  and device/inode/type revalidation. Active candidate bytes come from memory
  and must not reread the same destination.

- [ ] Filter `.terraform.lock.hcl`, `.terraform/providers`, and
  `.terraform/modules/modules.json` to the exact dependencies selected by the
  effective candidate configuration. Preserve the proof's fail-closed
  filesystem-function policy.

- [ ] Verify:

  ```bash
  python3 -m pytest tests/test_module_manifest.py -q
  python3 -m ruff check src/ubitofu/module_manifest.py \
    tests/test_module_manifest.py
  python3 -m mypy src/ubitofu
  ```

- [ ] Commit:

  ```bash
  git add src/ubitofu/module_manifest.py tests/test_module_manifest.py
  git commit -m "tofu: model the effective offline validation module"
  ```

---

### Task 2: Implement production offline staged validation

**Files:**

- Create: `src/ubitofu/staged_validation.py`
- Create: `tests/test_staged_validation.py`
- Modify: `src/ubitofu/tofu_runner.py`
- Modify: `docs/staged-validation-contract.md`
- Retain explicit integration suite: `proofs/test_staged_validation_proof.py`
- Retain Linux environment: `proofs/staged-validation-linux.Dockerfile`

**Interfaces:**

```python
@dataclass(frozen=True)
class ValidationResult:
    candidate_hashes: tuple[tuple[PurePosixPath, str], ...]
    platform_enforcement: Literal["macos-sandbox", "linux-user-netns"]

class StagedValidationError(UbitofuError):
    def __init__(self, phase: str, safe_reason: str) -> None: ...

def validate_staged_module(
    *,
    workdir: Path,
    manifest: EffectiveModuleManifest,
    private_root: Path,
    tofu_binary: str = "tofu",
) -> ValidationResult: ...
```

- [ ] Add default-suite unit tests for private modes, copies rather than
  hardlinks, exact manifest materialization, private `TF_DATA_DIR`, empty
  filesystem mirror CLI config, proxy poisoning, environment allowlisting,
  command arguments, bounded safe failures, and destination immutability.
  Mock only the final platform launcher in these unit tests.

- [ ] Add launcher tests that require the macOS probe sentinel/status and Linux
  parent-netns-denied plus network-denied sentinels/status. Missing binaries,
  denied namespace creation, unexpected errno, wrong status, or wrong sentinel
  must stop before OpenTofu.

- [ ] Confirm red:

  ```bash
  python3 -m pytest tests/test_staged_validation.py -q
  ```

- [ ] Implement materialization with new files, restrictive modes, file fsync,
  and directory fsync. Run only `tofu validate -no-color`; do not initialize,
  contact a backend, or execute provider API calls.

- [ ] Use a minimal environment containing `PATH`, locale, the proved OpenTofu
  automation flags, private data/config paths, and poisoned proxy variables.
  Do not forward credentials, cloud variables, or deployment `TF_VAR_*` values.

- [ ] Run the retained explicit proof on both supported enforcement routes and
  compare its result with production validation on the same fixture:

  ```bash
  python3 -m pytest tests/test_staged_validation.py -q
  python3 -m pytest proofs/test_staged_validation_proof.py -q -ra
  ```

  Use the documented retained container recipe for Linux. If the host cannot
  enforce the sandbox, record the lane as unrun; do not weaken the test.

- [ ] Update the contract with production module paths and supported-version
  policy while retaining the proof records and permission requirements.

- [ ] Verify Ruff and mypy:

  ```bash
  python3 -m ruff check src/ubitofu/staged_validation.py \
    tests/test_staged_validation.py
  python3 -m mypy src/ubitofu
  git diff --check
  ```

- [ ] Commit:

  ```bash
  git add src/ubitofu/staged_validation.py src/ubitofu/tofu_runner.py \
    tests/test_staged_validation.py docs/staged-validation-contract.md
  git commit -m "tofu: validate candidate modules offline before commit"
  ```

---

### Task 3: Define the durable transaction manifest and preparation

**Files:**

- Create: `src/ubitofu/file_transaction.py`
- Create: `tests/test_file_transaction.py`
- Modify: `src/ubitofu/errors.py`

**Interfaces:**

```python
class TransactionPhase(Enum):
    PREPARED = "prepared"
    COMMITTING = "committing"
    ROLLING_BACK = "rolling_back"
    COMMITTED = "committed"
    QUARANTINED = "quarantined"

class FileOperation(Enum):
    CREATE = "create"
    REPLACE = "replace"
    DELETE = "delete"

@dataclass(frozen=True)
class TransactionEntry:
    relative_path: PurePosixPath
    operation: FileOperation
    original_identity: FileIdentity | None
    original_sha256: str | None
    candidate_sha256: str | None
    backup_sha256: str | None
    original_mode: int | None
    candidate_mode: int | None

@dataclass(frozen=True)
class TransactionManifest:
    protocol_version: int
    transaction_id: str
    entries: tuple[TransactionEntry, ...]

class TransactionError(UbitofuError):
    def __init__(
        self,
        phase: TransactionPhase,
        affected_paths: tuple[PurePosixPath, ...],
        recovery_path: Path | None,
        safe_reason: str,
    ) -> None: ...

class PreparedTransaction:
    def validate_sources(self) -> None: ...
    def commit(self) -> None: ...

def prepare_transaction(
    *,
    workdir: Path,
    candidates: tuple[ProposedFile, ...],
) -> PreparedTransaction: ...
```

- [ ] Add tests for create/replace/delete, multi-file order, duplicate/escaping
  paths, workdir root targets, symlinks in every path component, sockets/FIFOs,
  existing mode preservation, documented mode for new files, cross-device
  staging, stale hash, inode replacement, size/mtime-only changes, and no-op
  candidates.

- [ ] Test manifest determinism and strict decoding: version, transaction ID,
  relative paths, operation/nullable-field consistency, duplicate entries,
  unknown phase, truncated JSON, oversized manifest, and unknown required
  fields. Never use pickle.

- [ ] Confirm red:

  ```bash
  python3 -m pytest tests/test_file_transaction.py -q
  ```

- [ ] Implement `.ubitofu/transactions/<random-id>` on the same filesystem as
  the workdir. Prepare complete private backups and candidate files using
  no-follow operations. Fsync every file before writing the manifest.

- [ ] Persist the manifest via a new temporary file, file fsync, atomic rename,
  and transaction-directory fsync. Persist phase and per-entry journal records
  with the same protocol. Use explicit JSON schemas and bounded reads.

- [ ] `validate_sources()` must recheck lstat identity, type, mode, size, mtime,
  and SHA-256 immediately before `COMMITTING`. Editors that do not honor the
  advisory lock must be caught.

- [ ] Verify:

  ```bash
  python3 -m pytest tests/test_file_transaction.py -q
  python3 -m ruff check src/ubitofu/file_transaction.py \
    tests/test_file_transaction.py
  python3 -m mypy src/ubitofu
  ```

- [ ] Commit:

  ```bash
  git add src/ubitofu/file_transaction.py src/ubitofu/errors.py \
    tests/test_file_transaction.py
  git commit -m "files: prepare fsynced candidates and recovery manifests"
  ```

---

### Task 4: Implement commit, rollback, and startup recovery

**Files:**

- Expand: `src/ubitofu/file_transaction.py`
- Expand: `tests/test_file_transaction.py`
- Create: `tests/helpers/transaction_crash_worker.py`
- Create: `tests/test_transaction_recovery.py`

**Durable protocol:**

1. `PREPARED`: candidates, complete backups, and manifest are durable; no
   destination has changed.
2. `COMMITTING`: before each entry, persist an `intent` journal record. Replace,
   create, or delete the destination, fsync its parent, then persist `applied`.
3. `COMMITTED`: every destination matches its candidate hash. Persist the phase,
   then remove only this transaction after verification.
4. `ROLLING_BACK`: walk applied or ambiguous intents in reverse. A destination
   matching candidate is restored; one matching original is already restored;
   any third value quarantines.
5. `QUARANTINED`: preserve the whole private transaction and block later
   mutation until an operator resolves it.

- [ ] Add injected-failure unit tests before and after each manifest, phase,
  intent, destination, applied, rollback, and cleanup fsync/rename boundary.
  Assert complete-new, complete-old, or quarantine according to the contract.

- [ ] Add subprocess SIGKILL tests for every durable boundary using
  `transaction_crash_worker.py`. On the next locked startup, run recovery and
  verify file contents, modes, hashes, transaction cleanup, or quarantine.

- [ ] Add recovery tests for PREPARED, COMMITTING with every mix of
  intent/applied records, ROLLING_BACK, COMMITTED, corrupt/truncated markers,
  missing candidates/backups, multiple residual transactions, external edits,
  failed rollback writes, and idempotent repeated recovery.

- [ ] Confirm red:

  ```bash
  python3 -m pytest tests/test_file_transaction.py \
    tests/test_transaction_recovery.py -q
  ```

- [ ] Implement deterministic destination order. For replace/create, write a
  same-directory temporary candidate then `os.replace`; for delete, preserve the
  complete backup before unlinking. Fsync every affected parent directory.

- [ ] Implement:

  ```python
  def recover_transactions(workdir: Path) -> tuple[RecoveryResult, ...]: ...
  ```

  It runs only under the workdir lock. Zero residual transactions is a no-op;
  one valid transaction recovers; multiple transactions, corrupt state, or
  hash ambiguity fail closed without choosing one.

- [ ] Make quarantine diagnostics name only safe workdir-relative affected paths
  and the private recovery directory. Never print backup contents.

- [ ] Verify:

  ```bash
  python3 -m pytest tests/test_file_transaction.py \
    tests/test_transaction_recovery.py -q
  python3 -m ruff check src/ubitofu/file_transaction.py \
    tests/test_file_transaction.py tests/test_transaction_recovery.py \
    tests/helpers/transaction_crash_worker.py
  python3 -m mypy src/ubitofu
  ```

- [ ] Commit:

  ```bash
  git add src/ubitofu/file_transaction.py tests/test_file_transaction.py \
    tests/test_transaction_recovery.py tests/helpers/transaction_crash_worker.py
  git commit -m "files: recover or quarantine interrupted commits"
  ```

---

### Task 5: Integrate validation and transactions into commands

**Files:**

- Modify: `src/ubitofu/pipeline.py`
- Modify: `src/ubitofu/reconcile_renderer.py`
- Modify: `tests/test_pipeline.py`
- Modify: `tests/test_reconcile.py`
- Modify: `tests/test_integration.py`
- Modify: `tests/test_cli.py`

- [ ] Add orchestration tests for recovery-before-snapshot, snapshot, plan,
  render, stage, internal validation, `tofu validate`, stale-source recheck,
  commit, report, and cleanup order. Inject a failure at each phase.

- [ ] Assert wet and check receive the same injected snapshot, equal
  `ReconcilePlan`, equal candidates, and equal validation result. Check must not
  call `prepare_transaction()` or create a commit-intent marker.

- [ ] Add generate tests proving imports, generated HCL, variables, stale bulk
  removal, and `COVERAGE.md` join one transaction. Verify uses only ephemeral
  phase-1 artifacts and performs no transaction.

- [ ] Confirm red:

  ```bash
  python3 -m pytest tests/test_pipeline.py tests/test_reconcile.py \
    tests/test_integration.py tests/test_cli.py -q
  ```

- [ ] Delete `_commit_candidates_legacy`. Compose the command exactly as:

  ```python
  with workdir_lock(workdir):
      recover_transactions(workdir)
      snapshot = collect_reconcile_snapshot(
          controller=controller,
          runner=runner,
          workdir=workdir,
          site=cfg.site,
      )
      plan = build_reconcile_plan(snapshot)
      candidates = render_candidates(plan, snapshot)
      manifest = build_effective_module_manifest(
          workdir=workdir,
          candidates=candidates,
      )
      validate_staged_module(
          workdir=workdir,
          manifest=manifest,
          private_root=artifact_paths.root / "validation",
      )
      if not check:
          transaction = prepare_transaction(workdir=workdir, candidates=candidates)
          transaction.validate_sources()
          transaction.commit()
  ```

- [ ] Render the report after successful validation. In wet mode, print the
  success report only after commit completes; on quarantine, print only the safe
  transaction error.

- [ ] Verify:

  ```bash
  python3 -m pytest tests/test_pipeline.py tests/test_reconcile.py \
    tests/test_integration.py tests/test_cli.py -q
  python3 -m ruff check src tests
  python3 -m mypy src/ubitofu
  git diff --check
  ```

- [ ] Commit:

  ```bash
  git add src/ubitofu/pipeline.py src/ubitofu/reconcile_renderer.py \
    tests/test_pipeline.py tests/test_reconcile.py tests/test_integration.py \
    tests/test_cli.py
  git commit -m "pipeline: validate and commit complete candidate trees"
  ```

---

### Task 6: Mutation-test and cross-platform-verify recovery

**Files:**

- Modify: `pyproject.toml`
- Modify: `ci/mutation_gate.py`
- Modify: `.woodpecker/ci.yml`
- Modify: `.github/workflows/ci.yml`
- Modify: `tests/test_infra_deps.py`
- Modify: `docs/testing.md`

- [ ] Add `module_manifest.py`, `staged_validation.py`, and
  `file_transaction.py` to mutation scope and CI path filters. Test infrastructure
  consistency before editing the workflow files.

- [ ] Kill meaningful mutations in phase comparison, hash checks, path
  containment, journal ordering, rollback direction, sandbox enforcement, and
  check/wet branching. Annotate only demonstrated equivalent mutants.

- [ ] Run default and explicit local gates:

  ```bash
  python3 -m pytest -m "not controller" --cov=ubitofu --cov-branch
  python3 -m ruff check .
  python3 -m mypy src
  python3 ci/mutation_gate.py check
  python3 -m pytest proofs/test_staged_validation_proof.py -q -ra
  git diff --check
  ```

- [ ] Run the retained Linux proof container and the production integration
  tests. Run process-kill recovery on macOS and Linux. Inspect fixture workdirs
  for plan files, transaction residue, unexpected modes, and leaked data.

- [ ] Measure `pipeline.py` complexity and dependencies. Remove it from mutation
  scope only if an automated assertion shows it is the thin composition root
  above; retain branch coverage regardless.

- [ ] Request an adversarial review focused on sandbox escape, manifest trust,
  symlink races, fsync ordering, crash ambiguity, rollback failure, concurrent
  editors, check/wet parity, and honest guarantee wording.

- [ ] Commit:

  ```bash
  git add pyproject.toml ci/mutation_gate.py .woodpecker/ci.yml \
    .github/workflows/ci.yml tests/test_infra_deps.py docs/testing.md
  git commit -m "ci: gate staged validation and crash recovery"
  ```

## Completion Gate

Phase 3 is complete only when the explicit macOS and Linux staged-validation
lanes pass with kernel enforcement, every durable process-kill boundary recovers
or quarantines as specified, wet/check plan and candidate parity is proved,
generate and reconcile have no persistent write outside the transaction, the
legacy write loop is deleted, and safety-module mutations have no unexplained
survivors.
