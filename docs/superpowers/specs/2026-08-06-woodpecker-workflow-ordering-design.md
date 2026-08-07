# Woodpecker Workflow Ordering

Approved for planning on 2026-08-06.

## Problem

The manual release-candidate pipeline currently runs the full mutation sweep
inside the `ci` workflow. The `controller` workflow depends on `ci`, so it does
not clone the private repository until every mutant has finished.

Pipeline 387 proved the consequence on commit
`ac33356fbfd0f197c0712110f359594a83394e86`: baseline tests and the full mutation
sweep passed, but the controller clone started roughly 91 minutes after the
pipeline began and received no HTTPS credential. No controller service or test
started. Retrying the pipeline would repeat the already-passed 12,861-mutant
sweep before reaching the same late clone boundary.

The proof gates need a deterministic order that clones each private workflow
early enough to run, keeps heavy work serialized, and does not add retry flags,
special proof modes, or another credential.

## Boundaries

This is repository CI architecture, not a ubitofu product feature. It does not
add CI-vendor behavior to the CLI or change the public command surface. It does
not change reconciliation, HCL generation, controller adapters, packaging, or
release behavior.

The change will not add compatibility paths, duplicate mutation commands,
browser automation, Ansible integration, tags, or releases.

## Design

Use three Woodpecker workflows with one dependency chain:

```text
ci -> controller -> mutation
```

`ci.yml` retains baseline tests, linting, type checks, secret scanning,
distribution builds, release steps, and the pull-request mutation gate. The
weekly and manual full mutation step moves out of this file.

`controller.yml` continues to own the seeded and simulated controller services
and their test step. It still depends on `ci`, so controller work does not start
after a failed baseline.

`mutation.yml` owns the full mutation sweep and depends on `controller`. Its
single test step runs only for manual and cron events. The mutation command and
80 percent threshold have one definition.

For manual and cron pipelines, the complete order is baseline tests and
gitleaks, controller tests, then the full mutation sweep. The controller clone
therefore occurs after the short baseline rather than after the long mutation
run. The mutation workflow receives its own clone after controller testing,
well before the delayed-clone boundary observed in pipeline 387.

For pull requests and tags, the existing `ci` and `controller` behavior remains
in force and `mutation` has no runnable step. Bare pushes continue to run only
the `ci` workflow. The controller and full sweep remain excluded from pushes.

The weekly cron path will run the same baseline and controller gates as a
manual release-candidate proof before mutation. This avoids conditional
dependencies or separate cron/manual mutation implementations. It also means a
weekly mutation score is never reported for a commit whose baseline or
controller proof failed.

## Timeout Contract

The repository's Woodpecker timeout is 120 minutes. The observed candidate run
used about 10 minutes for baseline work and 80 minutes for mutation, leaving the
remaining window for controller tests and workflow setup.

Woodpecker stores this timeout as repository configuration rather than workflow
YAML. Testing documentation will state the required setting and its CLI form so
a fork can reproduce the full manual or cron gate. The workflow will not carry
an unsupported `timeout` key or depend on private infrastructure configuration.

## Failure Behavior

Each workflow starts only after its dependency succeeds. A baseline failure
prevents controller and mutation work. A controller or controller-clone failure
prevents mutation. A mutation score below 80 percent fails the final workflow.

There is no automatic retry. Clone, controller, and mutation failures remain
distinct and visible in Woodpecker. Operators can rerun a failed pipeline after
fixing the cause, but the repository does not encode a second execution path.

## Verification

Static tests will enforce these invariants:

- The full mutation sweep exists only in `mutation.yml`.
- `controller` depends on `ci`, and `mutation` depends on `controller`.
- Manual and cron events run baseline, controller, and mutation gates.
- Bare pushes do not run controller or full mutation gates.
- The pull-request mutation scope still matches the configured module set.
- The 80 percent full-sweep threshold remains explicit.

After local lint, type, configuration, and focused test checks pass, push only
the candidate branch. A fresh manual Woodpecker pipeline for the exact candidate
commit must then show successful baseline, gitleaks, controller, and full
mutation workflows. Record every mutation result category and the final score.

Do not create a tag or release, and do not update Ansible as part of this work.
