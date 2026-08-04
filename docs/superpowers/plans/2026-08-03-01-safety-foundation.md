# ubitofu Safety Foundation Implementation Plan

Status: superseded by `2026-08-03-ubitofu-0.10-single-cutover.md`. Retained as
review history. Do not execute this phase independently.

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> superpowers:subagent-driven-development (recommended) or
> superpowers:executing-plans to implement this plan task-by-task. Steps use
> checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make every controller and OpenTofu input fail closed before extracting
the semantic planner.

**Architecture:** `controller.py` and `tofu_runner.py` remain the only external
I/O adapters. They return validated values or typed, safely renderable errors.
A POSIX workdir lock covers the full command, while one private artifact manager
owns every plan and generated stub. TLS policy is a deliberate breaking-release
task, not a silent default flip.

**Tech Stack:** Python 3.11+, `pathlib`, `fcntl`, `ssl`, `httpx`, OpenTofu CLI,
frozen dataclasses, pytest, Ruff, strict mypy, Hypothesis, mutmut.

## Global Constraints

- Preserve the plan-only boundary. Do not add controller writes, `tofu apply`,
  `tofu import`, state mutation, Git, backend, CI-vendor, or notification code.
- Preserve current human reports and outcome exits 0/10/11/12/13. Exit 1 is an
  operational error and exit 2 is CLI usage.
- Every new source file starts with the repository SPDX/copyright header.
- Write a failing test, observe the intended failure, implement the smallest
  behavior, and rerun focused tests before each commit.
- Never print raw stderr, response bodies, credentials, secret-bearing HCL,
  URL userinfo, ANSI escapes, or unbounded external output in normal mode.
- Never recursively remove an unresolved path, glob, workdir, or symlink target.
- The TLS-default task is released only with an explicit major-version boundary
  and its README/changelog migration note.

---

### Task 1: Establish typed safe diagnostics

**Files:**

- Create: `src/ubitofu/errors.py`
- Modify: `src/ubitofu/cli.py`
- Modify: `src/ubitofu/tofu_runner.py`
- Modify: `src/ubitofu/controller.py`
- Create: `tests/test_errors.py`
- Modify: `tests/test_cli.py`
- Modify: `tests/test_tofu_runner.py`
- Modify: `tests/test_controller.py`

**Interfaces:**

```python
class UbitofuError(RuntimeError):
    """Expected operational failure with safe operator context."""

class TofuExecutionError(UbitofuError):
    def __init__(self, command_kind: str, exit_code: int, safe_reason: str) -> None: ...

class TofuDocumentError(UbitofuError):
    def __init__(self, kind: str, safe_reason: str) -> None: ...

class ControllerResponseError(UbitofuError):
    def __init__(self, endpoint: str, status: int | None, safe_reason: str) -> None: ...

def render_safe_error(error: BaseException) -> str:
    """Return one bounded, control-character-free diagnostic line."""
```

- [ ] Add tests that construct each error and verify its public attributes and
  one-line rendering. Include secrets, URL userinfo, ANSI escapes, newlines,
  tabs, and a 100 KiB message in underlying exceptions; none may survive.

- [ ] Change `tests/test_cli.py::test_main_unexpected_error_surfaces_type_and_message`
  to require a stable incident-free message such as `unexpected internal error;
  rerun with local debug logging and report the command`, without exception text.

- [ ] Confirm the focused tests fail because current CLI and adapters expose raw
  exception/provider text:

  ```bash
  python3 -m pytest tests/test_errors.py tests/test_cli.py \
    tests/test_tofu_runner.py tests/test_controller.py -q
  ```

- [ ] Implement immutable public attributes on the typed errors and an
  allowlist-based renderer. Map expected errors directly in `main()` and map
  unexpected exceptions to the generic safe message. Do not add raw debug
  logging in this task.

- [ ] Rerun the focused tests, Ruff, and mypy:

  ```bash
  python3 -m pytest tests/test_errors.py tests/test_cli.py \
    tests/test_tofu_runner.py tests/test_controller.py -q
  python3 -m ruff check src/ubitofu/errors.py src/ubitofu/cli.py \
    src/ubitofu/tofu_runner.py src/ubitofu/controller.py tests/test_errors.py
  python3 -m mypy src/ubitofu
  ```

- [ ] Commit:

  ```bash
  git add src/ubitofu/errors.py src/ubitofu/cli.py src/ubitofu/tofu_runner.py \
    src/ubitofu/controller.py tests/test_errors.py tests/test_cli.py \
    tests/test_tofu_runner.py tests/test_controller.py
  git commit -m "errors: expose only safe operational diagnostics"
  ```

---

### Task 2: Interpret OpenTofu commands and JSON strictly

**Files:**

- Create: `src/ubitofu/tofu_json.py`
- Modify: `src/ubitofu/tofu_runner.py`
- Create: `tests/test_tofu_json.py`
- Modify: `tests/test_tofu_runner.py`

**Interfaces:**

```python
@dataclass(frozen=True)
class DocumentHeader:
    kind: Literal["plan", "state", "provider_schema"]
    format_major: int
    format_minor: int

def validate_document_header(
    value: object,
    *,
    kind: Literal["plan", "state", "provider_schema"],
) -> tuple[DocumentHeader, Mapping[str, object]]: ...

class TofuRunner:
    def plan(self, *, out: Path, generate_config_out: Path | None = None) -> int: ...
    def show_json(self, plan: Path) -> Mapping[str, object]: ...
    def show_state_json(self) -> Mapping[str, object]: ...
    def providers_schema(self) -> Mapping[str, object]: ...
```

- [ ] Add table tests for plan exits 0, 1, 2, 3; non-plan exits 0, 1, 2; missing
  or non-string `format_version`; supported `1.x`; unsupported major `2.x`;
  malformed JSON; missing/non-boolean `errored`; and `errored: true`.

- [ ] Replace
  `test_plan_generate_config_tolerates_nonzero_when_stub_written` with a test
  proving exit 1 rejects a non-empty stub and deletes it. A partial stub is
  diagnostic residue, never planner input.

- [ ] Confirm red:

  ```bash
  python3 -m pytest tests/test_tofu_json.py tests/test_tofu_runner.py -q
  ```

- [ ] Implement command-kind-specific exit handling. Only `plan
  -detailed-exitcode` may accept 0 or 2; `show`, state show, provider schema,
  `fmt`, and `validate` require 0. Decode JSON once at the adapter boundary and
  reject an errored plan even after a successful `show`.

- [ ] Preserve unknown minor-version fields. Do not yet model resource changes;
  phase 2 owns the exact deep-frozen subset.

- [ ] Verify:

  ```bash
  python3 -m pytest tests/test_tofu_json.py tests/test_tofu_runner.py \
    tests/test_pipeline.py tests/test_reconcile.py -q
  python3 -m ruff check src/ubitofu/tofu_json.py src/ubitofu/tofu_runner.py \
    tests/test_tofu_json.py tests/test_tofu_runner.py
  python3 -m mypy src/ubitofu
  ```

- [ ] Commit:

  ```bash
  git add src/ubitofu/tofu_json.py src/ubitofu/tofu_runner.py \
    tests/test_tofu_json.py tests/test_tofu_runner.py tests/test_pipeline.py \
    tests/test_reconcile.py
  git commit -m "tofu: reject failed and malformed plan documents"
  ```

---

### Task 3: Lock the workdir and contain ephemeral artifacts

**Files:**

- Modify: `.gitignore`
- Create: `src/ubitofu/workdir_lock.py`
- Create: `src/ubitofu/artifacts.py`
- Modify: `src/ubitofu/tofu_runner.py`
- Modify: `src/ubitofu/pipeline.py`
- Create: `tests/test_workdir_lock.py`
- Create: `tests/test_artifacts.py`
- Create: `tests/helpers/artifact_worker.py`
- Modify: `tests/test_pipeline.py`
- Modify: `tests/test_reconcile.py`
- Modify: `tests/test_integration.py`

**Interfaces:**

```python
@contextmanager
def workdir_lock(workdir: Path) -> Iterator[None]:
    """Hold an exclusive POSIX advisory lock for one workdir command."""

@dataclass(frozen=True)
class ArtifactPaths:
    root: Path
    plan: Path
    generated_hcl: Path

class ArtifactManager:
    @classmethod
    @contextmanager
    def open(cls, workdir: Path) -> Iterator[ArtifactPaths]: ...

    @classmethod
    def reclaim_residue(cls, workdir: Path) -> None: ...
```

- [ ] Add `.ubitofu/` to `.gitignore` before any runtime code creates it.

- [ ] Test mutual exclusion with a second process, release after normal and
  exceptional exits, mode 0700 for `.ubitofu`, `tmp`, and run directories, and
  rejection of symlinked control paths. PID text is diagnostic only and never
  proves ownership or staleness.

- [ ] Test exact-child artifact cleanup after plan success, plan exit 2, plan
  failure, JSON failure, caller failure, and SIGKILL. The next locked invocation
  must reclaim a valid abandoned child. Malformed entries, symlinks, or paths
  outside `.ubitofu/tmp` fail closed.

- [ ] Confirm red:

  ```bash
  python3 -m pytest tests/test_workdir_lock.py tests/test_artifacts.py \
    tests/test_pipeline.py tests/test_reconcile.py tests/test_integration.py -q
  ```

- [ ] Implement `fcntl.flock` without a state-machine dependency. Create a
  random direct child with `mkdir(mode=0o700)`, yield only resolved paths inside
  it, and remove only files recognized by the artifact manifest. Do not use a
  broad recursive delete.

- [ ] Move fixed `tf.plan`, `verify.plan`, and generated-stub ownership out of
  `pipeline.py`. Acquire the lock before residue recovery and snapshot I/O; hold
  it through artifact cleanup and command completion.

- [ ] Verify no plan or stub remains:

  ```bash
  python3 -m pytest tests/test_workdir_lock.py tests/test_artifacts.py \
    tests/test_tofu_runner.py tests/test_pipeline.py tests/test_reconcile.py \
    tests/test_integration.py -q
  find . -type f \( -name '*.plan' -o -name 'tf.plan' -o -name 'verify.plan' \) -print
  python3 -m ruff check src tests
  python3 -m mypy src/ubitofu
  ```

- [ ] Commit:

  ```bash
  git add .gitignore src/ubitofu/workdir_lock.py src/ubitofu/artifacts.py \
    src/ubitofu/tofu_runner.py src/ubitofu/pipeline.py \
    tests/test_workdir_lock.py tests/test_artifacts.py \
    tests/helpers/artifact_worker.py tests/test_pipeline.py \
    tests/test_reconcile.py tests/test_integration.py
  git commit -m "runtime: lock workdirs and contain private artifacts"
  ```

---

### Task 4: Validate controller transport and response policy

**Files:**

- Modify: `src/ubitofu/config.py`
- Modify: `src/ubitofu/controller.py`
- Modify: `src/ubitofu/coverage.py`
- Modify: `tests/fixtures/config.toml`
- Modify: `tests/test_config.py`
- Modify: `tests/test_controller.py`
- Modify: `tests/test_coverage.py`
- Modify: `tests/test_cli.py`
- Modify: `README.md`
- Modify: `CHANGELOG.md`

**Interfaces:**

```python
@dataclass(frozen=True)
class ControllerTls:
    verify_tls: bool = True
    ca_bundle: Path | None = None

@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 3
    total_deadline_seconds: float = 20.0

def build_tls_context(policy: ControllerTls) -> bool | ssl.SSLContext: ...

ABSENT_ENDPOINTS: Mapping[tuple[str, str], frozenset[int]]
```

- [ ] Add config tests for verified default, explicit insecure mode, a custom
  CA file, a missing CA file, and contradictory `verify_tls = false` plus
  `ca_bundle`. Use `ssl.create_default_context(cafile=...)`; never pass a CA
  filename directly as HTTPX `verify=`.

- [ ] Add `httpx.MockTransport` tests for 401/403, policy-listed and unlisted
  404/405, 429 with numeric and HTTP-date `Retry-After`, retry exhaustion,
  deadline exhaustion, 500, malformed JSON, and malformed collection envelopes.
  Retry idempotent GET only. Only a policy-listed absence becomes a coverage gap.

- [ ] Confirm red:

  ```bash
  python3 -m pytest tests/test_config.py tests/test_controller.py \
    tests/test_coverage.py tests/test_cli.py -q
  ```

- [ ] Implement the frozen TLS and retry policies, validated collection
  envelopes, and safe endpoint identifiers. Do not include full URLs in errors.

- [ ] Update README and CHANGELOG in the same change. State plainly that TLS
  verification now defaults on, show a custom-CA migration, and show the
  explicit insecure opt-out for controlled self-signed installations.

- [ ] Release this task only as the declared breaking version. Do not merge its
  code separately from the documentation and version decision.

- [ ] Verify:

  ```bash
  python3 -m pytest tests/test_config.py tests/test_controller.py \
    tests/test_coverage.py tests/test_cli.py -q
  python3 -m ruff check src tests
  python3 -m mypy src/ubitofu
  git diff --check
  ```

- [ ] Commit:

  ```bash
  git add src/ubitofu/config.py src/ubitofu/controller.py \
    src/ubitofu/coverage.py tests/fixtures/config.toml tests/test_config.py \
    tests/test_controller.py tests/test_coverage.py tests/test_cli.py \
    README.md CHANGELOG.md
  git commit -m "controller: verify TLS and classify responses precisely"
  ```

---

### Task 5: Expand the mutation and release gates

**Files:**

- Modify: `pyproject.toml`
- Modify: `ci/mutation_gate.py`
- Modify: `.woodpecker/ci.yml`
- Modify: `tests/test_infra_deps.py`
- Create: `docs/testing.md`

- [ ] Add an infrastructure test asserting that `tofu_json.py`,
  `workdir_lock.py`, and `artifacts.py` are in the per-PR mutation map and that
  the Woodpecker path filter matches that map.

- [ ] Add a non-mutating `check` mode to `ci/mutation_gate.py`, or guarantee its
  temporary `pyproject.toml` rewrite is restored in `finally`. Test that running
  the gate leaves tracked files byte-identical.

- [ ] Run each new module's mutations, kill meaningful survivors with behavior
  tests, and annotate only proven equivalent mutants with a line-level reason.

- [ ] Document fast local, full local, mutation, real-OpenTofu, and controller
  lanes without making Docker or a live controller part of the default suite.

- [ ] Run the phase gate:

  ```bash
  python3 -m pytest -m "not controller" --cov=ubitofu --cov-branch
  python3 -m ruff check .
  python3 -m mypy src
  python3 ci/mutation_gate.py check
  git diff --check
  ```

- [ ] Request an adversarial review focused on failed-plan acceptance, cleanup
  after process death, symlink escape, diagnostic leakage, TLS migration, and
  endpoint classification. Resolve critical and important findings.

- [ ] Commit:

  ```bash
  git add pyproject.toml ci/mutation_gate.py .woodpecker/ci.yml \
    tests/test_infra_deps.py docs/testing.md
  git commit -m "ci: gate the external safety boundaries"
  ```

## Completion Gate

Phase 1 is complete only when all default gates pass, focused real-OpenTofu
tests leave no plan/stub artifacts, process-death cleanup is demonstrated, the
new safety modules have no unexplained mutation survivors, and the TLS change
has an explicit breaking-release decision. Record unavailable live-controller
coverage as unrun; a skip is not evidence.
