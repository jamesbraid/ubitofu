# 0.10.1 Provider Contract Port Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task with verification checkpoints.

**Goal:** Port provider-contract admission and its DNS differential corpus into the released 0.10 architecture without restoring any retired command or semantic path.

**Architecture:** A context-managed provider execution boundary validates the exact contract evidence, owns the scoped provider override, and creates the one `TofuRunner` and schema consumed by each provider-backed command. The differential corpus is rewritten to call 0.10 semantic functions, not copied legacy algorithms.

**Tech Stack:** Python 3.11–3.14, OpenTofu, pytest, tree-sitter HCL, existing typed receipt and runtime-session modules, `mutmut`.

## Global Constraints

- Keep the 0.10 public commands and exit codes unchanged.
- Keep `reconcile --dry-run` and its lock/recovery semantics unchanged.
- Contract mode is opt-in. All four contract keys are required together.
- No pre-0.10 compatibility aliases, old pipeline, old reporter, or duplicate planner.
- Never capture provider/controller secrets into HCL, diagnostics, receipts, fixtures, or commit metadata.
- Relative contract paths resolve from the configuration file directory.
- Contract failures return exit 2 before controller construction or worktree writes.
- `uv.lock` is user-owned and remains untracked.

---

### Task 1: Add typed contract configuration and execution boundary

**Files:**
- Create: `src/ubitofu/provider_contract.py`
- Modify: `src/ubitofu/config.py`
- Modify: `src/ubitofu/tofu_runner.py`
- Test: `tests/test_provider_contract.py`
- Test: `tests/test_config.py`
- Test: `tests/test_tofu_runner.py`

**Interfaces:**
- `Config` gains only `provider_contract`, `provider_contract_checksum`, `provider_binary`, and `provider_schema_cli`.
- `provider_execution(*, cfg: Config, workdir: Path)` yields `ProviderExecution` and cleans all private scope on exit.
- `ProviderExecution.runner(*, workdir: Path, plan_path: Path | None = None)` returns a configured `TofuRunner`.
- `TofuRunner` accepts optional explicit `environment` and cached provider schema inputs without changing default behavior.

- [ ] **Step 1: Write failing configuration tests.**

Add tests that reject a partial four-key bundle, resolve relative paths from the config file directory, accept an all-absent bundle, and reject the removed claimed-schema/version/hash fields.

- [ ] **Step 2: Run the configuration tests and verify the expected failures.**

Run: `.venv/bin/python -m pytest tests/test_config.py -q`

Expected: new tests fail because the four fields and path-aware validation do not exist.

- [ ] **Step 3: Write failing execution-boundary tests.**

Cover sidecar checksum parsing, canonical provider schema hashing, executable identity, provider binary selection, cleanup after success and failure, cached-schema detachment, and safe `ProviderContractError` messages. Monkeypatch controller construction and assert contract failures never call it.

- [ ] **Step 4: Run the focused contract tests and verify they fail for missing APIs.**

Run: `.venv/bin/python -m pytest tests/test_provider_contract.py -q`

Expected: import/API failures, not fixture or assertion mistakes.

- [ ] **Step 5: Implement the minimal typed configuration and evidence resolver.**

Adapt the validation-only portions of the pre-0.10 contract implementation.
Resolve actual executables, copy only the selected provider into a mode-0700
temporary directory, construct the CLI dev override, query version/schema,
canonicalize the selected provider projection, and validate
contract/manifest/lifecycle identities. Keep the temporary-directory owner
inside the context object.

- [ ] **Step 6: Add runner environment and verified-schema plumbing.**

Make `_exec` pass the explicit environment and make `providers_schema()` return a detached cached document when contract execution supplies one. Keep command guards and all default `tofu` behavior intact.

- [ ] **Step 7: Run focused tests until green.**

Run: `.venv/bin/python -m pytest tests/test_config.py tests/test_provider_contract.py tests/test_tofu_runner.py -q`

Expected: all focused tests pass with no leaked temporary paths or secret values.

- [ ] **Step 8: Commit the boundary.**

Commit: `provider: admit one verified execution contract`

### Task 2: Port the differential corpus to 0.10 semantics

**Files:**
- Create: `src/ubitofu/contract_diff.py`
- Create: `src/ubitofu/dns_record_contract_v2.json`
- Create: `tests/fixtures/provider_contract/dns_record_v2.json`
- Modify: `tests/test_provider_contract.py`
- Test: `tests/test_contract_diff.py`
- Inspect: `src/ubitofu/generate.py`, `src/ubitofu/controller_projection.py`, `src/ubitofu/reconcile_planner.py`, `src/ubitofu/reconcile_renderer.py`, `src/ubitofu/outcomes.py`

**Interfaces:**
- `require_dns_corpus_parity(contract, corpus_path, provider_schema)` remains a contract admission check over corpus format 2.
- Corpus evaluation calls 0.10 normalization, projection, generation, reconciliation, secret suppression, and receipt-digest helpers.
- No evaluator imports retired modules or defines a second planner, renderer, secret scanner, or digest algorithm.

- [ ] **Step 1: Write a failing corpus guard test.**

Assert that the evaluator imports only current 0.10 modules and that every packaged case supplies the immutable 0.10 snapshot facts needed by the planner and produces the declared outcome, import IDs, redacted paths, generated HCL digest, and receipt-input digest.

- [ ] **Step 2: Run the corpus test to establish the red failure.**

Run: `.venv/bin/python -m pytest tests/test_contract_diff.py -q`

Expected: module/corpus evaluator is absent.

- [ ] **Step 3: Port the corpus evaluator through pure 0.10 adapters.**

Build immutable format-2 fixture inputs for absent, defaulted, configured, imported, drifted, sensitive, unsupported, and no-op cases. Call the real provider-schema normalization and semantic functions. Preserve the case identities and re-baseline expected native-HCL and 0.10 receipt digests.

- [ ] **Step 4: Run the corpus tests and inspect all digest mismatches.**

Run: `.venv/bin/python -m pytest tests/test_contract_diff.py tests/test_provider_contract.py -q`

Expected: all cases pass without importing deleted legacy modules.

- [ ] **Step 5: Commit the native differential proof.**

Commit: `provider: port the contract differential corpus`

### Task 3: Integrate execution into provider-backed commands

**Files:**
- Modify: `src/ubitofu/pipeline.py`
- Modify: `src/ubitofu/cli.py`
- Modify: `src/ubitofu/generate.py`
- Modify: `src/ubitofu/inspect.py`
- Modify: `src/ubitofu/plan_check.py`
- Modify: `tests/test_cli.py`
- Modify: `tests/test_pipeline.py`
- Add tests: `tests/test_provider_execution_integration.py`

**Interfaces:**
- `generate`, `reconcile`, `check`, and `inspect` receive runners only from `ProviderExecution`.
- Health commands never run provider admission.
- Contract failures map through the existing safe error renderer to exit 2 before controller construction.

- [ ] **Step 1: Write failing integration tests.**

Test contract and non-contract dispatch for each provider-backed command, exact runner reuse, dry-run behavior, controller-not-called-on-contract-failure, and health-command bypass.

- [ ] **Step 2: Run the integration tests and verify red.**

Run: `.venv/bin/python -m pytest tests/test_provider_execution_integration.py tests/test_cli.py tests/test_pipeline.py -q`

Expected: current pipeline signatures construct ordinary runners and do not invoke provider execution.

- [ ] **Step 3: Add the composition boundary.**

Enter provider execution after the relevant runtime lock is acquired and before controller construction. Thread the execution-created runner and cached schema through the existing generation, reconciliation, saved-plan check, and inspection calls. Leave health paths unchanged.

- [ ] **Step 4: Run the integration suite green.**

Run: `.venv/bin/python -m pytest tests/test_provider_execution_integration.py tests/test_cli.py tests/test_pipeline.py -q`

Expected: all command paths pass and `reconcile --dry-run` still writes no worktree files.

- [ ] **Step 5: Commit the command integration.**

Commit: `pipeline: use verified provider execution`

### Task 4: Version, documentation, and release candidate proof

**Files:**
- Modify: `pyproject.toml`
- Modify: `CHANGELOG.md`
- Modify: `README.md`
- Modify: `docs/testing.md`
- Modify: `tests/test_packaging.py`
- Modify: `tests/test_infra_deps.py`

- [ ] **Step 1: Write failing packaging/documentation contract tests.**

Assert package version `0.10.1`, packaged contract corpus presence, configuration examples, and no old command or claimed-schema compatibility fields in the public documentation.

- [ ] **Step 2: Run the packaging tests and verify red.**

Run: `.venv/bin/python -m pytest tests/test_packaging.py tests/test_infra_deps.py -q`

Expected: version and package-data assertions fail against 0.10.0.

- [ ] **Step 3: Update version, changelog, operator docs, and package data.**

Document contract mode as an optional preflight, its all-or-none keys, path rules, failure exit 2, and the fact that it never applies or mutates state. Do not describe old commands.

- [ ] **Step 4: Run all static and focused checks.**

Run each command separately: `.venv/bin/ruff check .`, `.venv/bin/mypy src`,
`.venv/bin/python ci/mutation_gate.py check`, the focused pytest command, and
`gitleaks detect --source . --config .gitleaks.toml --redact --verbose --exit-code 1`.

Expected: all commands exit 0 and gitleaks reports no leaks.

- [ ] **Step 5: Run the complete local suite with required network/socket access.**

Run: `.venv/bin/python -m pytest -q`

Expected: the configured non-controller suite passes. Controller proof remains
a Woodpecker-only verification.

- [ ] **Step 6: Push the candidate branch and run serialized Woodpecker proofs.**

Run the ordinary branch gate, then one manual serialized workflow. Confirm baseline, gitleaks, controller tests, mutation totals/categories, package artifacts, and absence of private material before proposing a 0.10.1 tag.

- [ ] **Step 7: Commit the release preparation.**

Commit: `release: prepare v0.10.1 provider contract port`
