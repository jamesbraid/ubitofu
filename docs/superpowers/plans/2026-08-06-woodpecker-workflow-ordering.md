# Woodpecker Workflow Ordering Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Serialize baseline, controller, and full mutation proof workflows so every private clone starts before the delayed-auth boundary.

**Architecture:** Keep `ci`, `controller`, and full mutation testing as three Woodpecker workflows joined by `ci -> controller -> mutation`. Event filters make the complete chain run for manual and cron proofs while bare pushes retain only the baseline workflow.

**Tech Stack:** Woodpecker 3.x workflow YAML, pytest, Python 3.11+, Markdown

## Global Constraints

- Keep the full mutation command and 80 percent threshold in one workflow.
- Keep heavy workflows serialized because the Woodpecker server uses SQLite.
- Do not add retry flags, proof-only CLI modes, credentials, compatibility paths, or duplicate commands.
- Do not change ubitofu's public command surface or production Python modules.
- Do not run a full mutation sweep locally.
- Push only `emdash/architecture-review-6gwp3`.
- Do not create a tag or release, and do not update Ansible.
- Preserve the untracked `uv.lock` without staging or deleting it.

---

### Task 1: Enforce and implement the workflow chain

**Files:**
- Create: `.woodpecker/mutation.yml`
- Modify: `.woodpecker/ci.yml`
- Modify: `.woodpecker/controller.yml`
- Modify: `tests/test_infra_deps.py`

**Interfaces:**
- Consumes: Woodpecker workflow filenames as dependency names and `ci/mutation_gate.py sweep --threshold 80` as the existing full-sweep entry point.
- Produces: the workflow dependency contract `ci -> controller -> mutation` and `_woodpecker_step(document: str, name: str) -> str` for focused static assertions.

- [ ] **Step 1: Add a failing workflow-contract test**

Add this helper and test to `tests/test_infra_deps.py`:

```python
def _woodpecker_step(document: str, name: str) -> str:
    marker = f"  - name: {name}\n"
    start = document.index(marker)
    end = document.find("\n  - name: ", start + len(marker))
    return document[start:] if end == -1 else document[start:end]


def test_full_proof_workflows_are_serialized_without_duplicate_sweeps() -> None:
    repository = Path(__file__).resolve().parents[1]
    workflows = repository / ".woodpecker"
    ci = (workflows / "ci.yml").read_text()
    controller = (workflows / "controller.yml").read_text()
    mutation = (workflows / "mutation.yml").read_text()
    sweep = "python ci/mutation_gate.py sweep --threshold 80"

    assert "depends_on:\n  - ci\n" in controller
    assert "depends_on:\n  - controller\n" in mutation
    assert "  - name: mutation-sweep\n" not in ci
    assert sum(document.count(sweep) for document in (ci, controller, mutation)) == 1
    assert "event: [push, pull_request, tag, manual, cron]" in _woodpecker_step(
        ci, "test"
    )
    assert "event: [push, pull_request, tag, manual, cron]" in _woodpecker_step(
        ci, "gitleaks"
    )
    assert "event: [pull_request, tag, manual, cron]" in _woodpecker_step(
        controller, "controller-tests"
    )
    assert "event: [cron, manual]" in _woodpecker_step(
        mutation, "mutation-sweep"
    )
```

- [ ] **Step 2: Run the focused test and confirm the red state**

Run:

```console
.venv/bin/python -m pytest tests/test_infra_deps.py::test_full_proof_workflows_are_serialized_without_duplicate_sweeps -q
```

Expected: FAIL because `.woodpecker/mutation.yml` does not exist.

- [ ] **Step 3: Create the mutation workflow**

Create `.woodpecker/mutation.yml` with one full-sweep definition:

```yaml
---
# Weekly and manual full mutation backstop. Keep this behind controller proof
# so every reported mutation score belongs to a candidate whose baseline and
# live-controller gates passed.

depends_on:
  - controller

steps:
  - name: mutation-sweep
    image: python:3.11-bookworm
    commands:
      - bash ci/install-tofu.sh
      - pip install -e ".[dev]"
      - python ci/mutation_gate.py sweep --threshold 80
    when:
      event: [cron, manual]
```

- [ ] **Step 4: Remove the full sweep from `ci.yml` and enable its prerequisites for cron**

Delete the `mutation-sweep` step and its cron-creation comment from
`.woodpecker/ci.yml`. Change the `test` and `gitleaks` filters to:

```yaml
event: [push, pull_request, tag, manual, cron]
```

Update the file header so `ci.yml` describes baseline, package, release, and
per-PR mutation gates rather than claiming ownership of the full sweep.

- [ ] **Step 5: Make controller proof part of manual and cron chains**

Change the controller step filter to:

```yaml
event: [pull_request, tag, manual, cron]
```

Update the workflow comment to state that `controller` runs after `ci` and
before `mutation`, keeping one heavy workflow active at a time.

- [ ] **Step 6: Run the focused test and mutation configuration check**

Run:

```console
.venv/bin/python -m pytest tests/test_infra_deps.py::test_full_proof_workflows_are_serialized_without_duplicate_sweeps -q
.venv/bin/python ci/mutation_gate.py check
```

Expected: PASS for both commands.

- [ ] **Step 7: Commit the workflow contract**

Stage only the four task files. Use the repository's component-prefixed,
imperative commit style:

```text
ci: serialize controller and mutation proofs
```

The body must record that the old order delayed the controller's private clone
until Woodpecker no longer supplied its HTTPS credential.

---

### Task 2: Document the reproducible timeout and proof commands

**Files:**
- Modify: `docs/testing.md`
- Modify: `tests/test_infra_deps.py`

**Interfaces:**
- Consumes: Woodpecker's repository-level `--timeout 2h` setting and the existing candidate branch naming convention.
- Produces: an operator-visible recipe for configuring and starting the complete proof without a browser.

- [ ] **Step 1: Add a failing documentation-contract test**

Add this test to `tests/test_infra_deps.py`:

```python
def test_full_proof_timeout_and_cli_trigger_are_documented() -> None:
    repository = Path(__file__).resolve().parents[1]
    testing = (repository / "docs" / "testing.md").read_text()

    assert "repo update --timeout 2h <owner/repo>" in testing
    assert "pipeline create --branch <candidate-branch> <owner/repo>" in testing
    assert "ci -> controller -> mutation" in testing
```

- [ ] **Step 2: Run the documentation test and confirm the red state**

Run:

```console
.venv/bin/python -m pytest tests/test_infra_deps.py::test_full_proof_timeout_and_cli_trigger_are_documented -q
```

Expected: FAIL because `docs/testing.md` does not yet contain the repository
timeout or manual pipeline commands.

- [ ] **Step 3: Expand the mutation-testing documentation**

Add a Woodpecker proof subsection to `docs/testing.md` that states:

- Full local Mutmut runs remain prohibited.
- Manual and cron proofs execute `ci -> controller -> mutation`.
- Forks need a two-hour repository timeout because Woodpecker's timeout is not a workflow YAML key.
- The CLI-only setup and trigger commands are:

```console
woodpecker-cli --disable-update-check repo update --timeout 2h <owner/repo>
woodpecker-cli --disable-update-check pipeline create \
  --branch <candidate-branch> <owner/repo>
```

Retain the existing `ci/mutation_gate.py check` local command and explain that
the final server log must report total, killed, survived, timeout, and all
zero-valued abnormal result categories.

- [ ] **Step 4: Run focused tests and prose checks**

Run:

```console
.venv/bin/python -m pytest tests/test_infra_deps.py -q
python3 /Users/jamesb/.agents/skills/prose-style/scripts/check_prose.py docs/testing.md
git diff --check
```

Expected: all commands PASS.

- [ ] **Step 5: Commit the operator contract**

Stage only `docs/testing.md` and `tests/test_infra_deps.py`. Use:

```text
docs,tests: document the full Woodpecker proof
```

The body must explain that the two-hour setting makes the checked-in serialized
proof reproducible without embedding instance settings in workflow YAML.

---

### Task 3: Verify the implementation locally

**Files:**
- Verify only. Modify task files only if a failing check identifies a defect.

**Interfaces:**
- Consumes: the three-workflow contract and documentation from Tasks 1 and 2.
- Produces: local evidence suitable for deciding whether to push the candidate branch.

- [ ] **Step 1: Run infrastructure and configuration tests**

```console
.venv/bin/python -m pytest tests/test_infra_deps.py -q
.venv/bin/python ci/mutation_gate.py check
```

Expected: PASS.

- [ ] **Step 2: Run static checks**

```console
.venv/bin/python -m ruff check .
.venv/bin/python -m mypy src
git diff --check
```

Expected: PASS.

- [ ] **Step 3: Run the local non-controller suite**

```console
.venv/bin/python -m pytest -q
```

Expected: PASS with only established platform or environment skips. Do not
start Mutmut locally.

- [ ] **Step 4: Audit the candidate state**

```console
git status --short --branch
git log -3 --oneline
git tag --list 'v0.10*'
```

Expected: only the preserved untracked `uv.lock`, the intended local commits,
and no `v0.10` tag.

---

### Task 4: Push and prove the exact candidate on Woodpecker

**Files:**
- No worktree changes expected.

**Interfaces:**
- Consumes: the verified candidate commit and Woodpecker repository timeout of two hours.
- Produces: exact-SHA server evidence for baseline, controller, and mutation gates.

- [ ] **Step 1: Push only the candidate branch**

```console
git push origin emdash/architecture-review-6gwp3
```

Expected: the remote branch advances to the verified local HEAD. Do not push
tags or another branch.

- [ ] **Step 2: Verify the ordinary push pipeline**

Use `woodpecker-cli pipeline ls` and `pipeline ps` to confirm the push pipeline
runs baseline and gitleaks gates without controller or full mutation work.

- [ ] **Step 3: Start a fresh manual proof**

```console
woodpecker-cli --disable-update-check pipeline create \
  --branch emdash/architecture-review-6gwp3 infra/ubitofu
```

Expected workflow order: `ci`, then `controller`, then `mutation`.

- [ ] **Step 4: Monitor every workflow to terminal state**

Use `pipeline ps` and `pipeline log show`. Confirm controller clone and tests
finish before mutation starts. Do not use a browser.

- [ ] **Step 5: Record the mutation verdict**

Confirm all 12,861 generated mutants complete unless the exact implementation
changes the generated count. Record total, killed, survived, timeout, no-tests,
suspicious, skipped, segfault, and interrupted/not-checked values. Require a
score of at least 80 percent.

- [ ] **Step 6: Perform the final remote and release audit**

Verify local HEAD equals both candidate remotes, `git status` contains only
`uv.lock`, and no local or remote `v0.10` tag exists. Do not tag, release, or
update Ansible.
