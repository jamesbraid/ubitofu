# Testing ubitofu

ubitofu supports CPython 3.11 through 3.14 on macOS and Linux. The CI release
matrix runs every supported Python version on Linux x86-64, Python 3.11 and 3.14
on macOS arm64, and Python 3.11 on Linux arm64. Windows is unsupported because
the worktree lock depends on `fcntl`.

OpenTofu contract and integration tests target OpenTofu 1.12.x. The JSON adapter
accepts format major version 1 and validates every field ubitofu uses.

## Local verification

Install the development dependencies into an isolated environment:

```console
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev,controller]'
```

Run the default local gate:

```console
.venv/bin/python -m ruff check .
.venv/bin/python -m mypy src
.venv/bin/python -m pytest -q
.venv/bin/python -m pytest proofs/test_hcl_parser_proof.py -q
.venv/bin/python ci/mutation_gate.py check
git diff --check
```

The repository's pytest configuration excludes tests marked `controller` from
the default run. A focused command may name the files under active development,
for example:

```console
.venv/bin/python -m pytest tests/test_cli.py tests/test_pipeline.py \
  tests/test_integration.py tests/test_reconcile_planner.py \
  tests/test_reconcile_renderer.py -q
```

## Package verification

Build both distribution forms and test installation from the resulting
artifacts before preparing a release:

```console
.venv/bin/python -m build
.venv/bin/python -m pytest tests/test_packaging.py -q
```

The boundary CI jobs install the wheel and source distribution rather than
testing only an editable checkout.

## Controller scenarios

Controller tests live under `tests/controllertest` and require either a pinned
testcontainers image or an explicitly supplied controller. They are marked
`controller`. UniFi OS Server-only cases also carry `uos`.

Run all configured controller scenarios with:

```console
.venv/bin/python -m pytest -m controller tests/controllertest -q
```

The controller contract uses flavor-specific environment variables such as
`UNIFI_TEST_<FLAVOR>_URL`, `UNIFI_TEST_<FLAVOR>_IMAGE`,
`UNIFI_TEST_EXPECT_VERSION`, and `UNIFI_TEST_REQUIRE`. See the fixtures in
`tests/controllertest` for the accepted flavor names and required credentials.
Unavailable scenarios are unrun, not counted as passing evidence.

Use `UNIFI_TEST_SEEDED_URL` with real hardware only on a trusted, isolated test
network. The test harness disables TLS certificate verification and may send
administrator credentials to that URL. Production ubitofu still verifies TLS
by default.

The ordinary deterministic suite covers no-op reconciliation, UI-only capture,
independent HCL and UI changes, a same-field conflict, forbidden device
creation, endpoint absence, and health degradation using immutable snapshots or
mock transports. These tests are not live-controller evidence.

The controller suite currently proves the harness and controller connectivity.
Its no-op and write scenarios remain parked on the provider import defects
tracked in `docs/provider-import-bugs.md`. Unavailable or parked scenarios are
unrun, not passing release evidence.

## Mutation testing

Full mutation testing runs only in Woodpecker. Do not start a full local mutmut
run: it is expensive and its worker artifacts are easy to leave behind. Local
work uses focused pytest commands and the non-mutating consistency check:

```console
.venv/bin/python ci/mutation_gate.py check
```

That command verifies that the pyproject mutation scope, per-change module
selection, and Woodpecker path filters agree. The server gate runs the actual
mutants for changed correctness modules.

### Woodpecker proof

Manual and cron proofs run one serialized workflow chain:

```text
ci -> controller -> mutation
```

The baseline and live-controller gates must pass before the full mutation
sweep starts. Bare pushes run only the baseline workflow. Full Mutmut runs
remain server-only.

Woodpecker stores the pipeline timeout as repository configuration rather than
workflow YAML. Set the two-hour proof window once for each repository or fork,
then start a manual candidate proof from the CLI:

```console
woodpecker-cli --disable-update-check repo update --timeout 2h <owner/repo>
woodpecker-cli --disable-update-check pipeline create --branch <candidate-branch> <owner/repo>
```

The optional weekly backstop uses the same chain:

```console
woodpecker-cli --disable-update-check cron add --repo <owner/repo> \
  --name mutation-weekly --expr "0 2 * * 1" --branch main
```

Inspect the terminal mutation log rather than only the workflow headline. It
must report total, killed, survived, timeout, no-tests, suspicious, skipped,
segfault, and interrupted or not-checked results. The gate requires a mutation
score of at least 80 percent.

## Cutover contract checks

Before declaring the 0.10 command switch complete, confirm that removed runtime
paths survive only in historical changelog text or explicit removal tests:

```console
rg -n 'run_reconcile\(.*check|--check|cmd_verify|cmd_enumerate|hcl_surgeon|reporter' \
  src tests README.md pyproject.toml
```

Also exercise the CLI and receipt tests. They prove the seven-operation public
surface, common 0/1/2/3 exits, owner-only mode-0600 output, output/input collision
checks, and human/JSON projections of the same typed result.
