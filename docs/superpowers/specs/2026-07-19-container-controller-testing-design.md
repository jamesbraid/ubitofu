# Container-controller integration testing — Design

Date: 2026-07-19
Status: approved (rev 2 — convergence review from the go-unifi
controller-testing session integrated; see
`2026-07-19-container-controller-testing-design-review.md`)

**Normative contract:** `docs/testing-contract.md` on the
`testing-contract` branch of github.com/jamesbraid/unifi-containers is the
source of truth for harness semantics shared by go-unifi (reference
implementation, Go), ubitofu (this spec, Python), and the terraform
provider (planned). Where this spec and the contract disagree, the
contract wins.

## Mission

Live-controller integration coverage for the reconcile-errors work shipped
in v0.5.0, plus the harness every future scenario builds on. The behaviors
under test:

1. **hcl_surgeon depth-balance fix** — scalar drift below a multiline
   collection value must still merge (pre-fix, every later top-level attr
   became invisible to the scanner).
2. **classify_diverged** — controller-deleted resources classify as
   `deleted`, committed-but-never-applied ones as `pending`, via live
   identities and state identities.
3. **rsync-style outcome exit codes** — 0 in sync, 10 drift captured, 11
   attention required, 12 both, 1 error — across reconcile and verify.

Today's suite covers all of this with fixtures and fakes. This work adds
the missing layer: real controllers, the real provider, real `tofu apply`,
real API mutations — end to end.

## Test targets

All images come from `ghcr.io/jamesbraid/unifi-containers` — version-
pinned, multi-arch, healthcheck-means-API-answers, published. Consumer CI
pulls, never builds; a missing tag for the pin is a red check, never a
fallback build. One version pin per flavor lives in the harness
(env-overridable via `UNIFI_TEST_<FLAVOR>_IMAGE`); everything else derives
from it.

| Fixture | Image | Boot | Credentials | Role |
|---|---|---|---|---|
| `seeded_controller` | `ghcr.io/jamesbraid/unifi-network:10.4.57-seeded` | ~15 s | `admin` / `unifi-containers-seeded` | Write-E2E scenarios — the only flavor writes are allowed on. Real empty site, wizard pre-completed at image build. |
| `sim_controller` | `ghcr.io/jamesbraid/unifi-network:10.4.57-sim` | ~15–25 s | `admin` / `admin` | Device scenarios. Simulation seeds 3 APs, 1 gateway, 5 switches with real MACs. API test double: reads and device topology only; write fidelity unspecified per version. |
| `uos_controller` | `ghcr.io/jamesbraid/unifi-os-server:5.1.21-sim` | 1–5 min | `admin` / `admin` | UOS-native dialect regression: `/proxy/network` + `X-API-KEY` on port 443 — ubitofu's production dialect. |

The UOS container requires the documented runtime contract (cap list, host
cgroupns with `/sys/fs/cgroup` rw, tmpfs set) — canonical form
`unifi-os/examples/docker-compose.yml` in unifi-containers; the harness
passes it via testcontainers `with_kwargs`.

Harnesses never mount `/unifi` or any controller state; teardown removes
the container and its anonymous volumes.

## Product change: classic-dialect support in controller.py

`controller.py` currently speaks only the UniFi OS dialect: `/proxy/network`
path prefix, `X-API-KEY` auth. The classic standalone controller (both
network images, and even the UOS direct port 7443) uses unprefixed paths
(`/api/s/<site>/rest/...`, `/v2/api/site/<site>/...`) and cookie login
(`POST /api/login`, username/password).

ubitofu grows a config-driven dialect:

- `dialect = "unifi-os"` (default, current behavior) | `"classic"`.
- Classic dialect drops the `/proxy/network` prefix and authenticates by
  cookie login with `username`/`password` config keys (secret handling per
  the existing api-key source machinery).
- TDD'd against the live seeded container.

This closes the self-hosted gap the README already claims to support. It
is a prerequisite for every classic-image scenario, so it lands early.

## Harness

Module name **`controllertest`** (`tests/controllertest/`) — the canonical
harness name in every language per the contract.

- **testcontainers-python** (`testcontainers>=4.14`) in a new optional
  dependency group `controller` (not `dev` — mutation and lint envs never
  need docker deps).
- Session-scoped fixtures per flavor: `DockerContainer` +
  `HealthcheckWaitStrategy`; per-flavor startup timeouts (sim/seeded
  ≥ 5 min to absorb first pull; UOS honors its 10-minute start period).
  The readiness contract lives in the image healthcheck.
- **URL mode**: `UNIFI_TEST_SEEDED_URL`, `UNIFI_TEST_SIM_URL`,
  `UNIFI_TEST_UOS_URL` bypass container boot and target an existing
  controller (this is how Woodpecker services plug in). Readiness is a
  bounded login poll with the contract's criterion: ready ⇔ the login
  endpoint returns a **JSON body with `rc == "ok"`**. HTTP 200 alone is
  never sufficient — during early boot the controller answers every path,
  login included, with an HTML placeholder page and HTTP 200. Connection
  errors and non-JSON bodies mean "still booting", retry; JSON
  `rc != "ok"` or an HTTP error status is a real rejection and fails
  immediately. Covered by a fast unit test flipping a fake server from
  placeholder to JSON (go-unifi precedent: `controllertest/session.go`,
  `controller_test.go`).
- On readiness timeout, dump the container log tail before failing.
- `UNIFI_TEST_KEEP=1` skips teardown for debugging (reaper backstop still
  applies otherwise). `UNIFI_TEST_<FLAVOR>_IMAGE` overrides an image pin.
- `seeder.py`: thin, independent cookie-login httpx client — never the
  code under test — that creates/mutates/deletes controller objects
  (networks, WLANs, users, sites) plus `cmd/sitemgr` helpers (`add-site`,
  `delete-device`). **A failed seed fails the scenario; it must never
  decay into an empty-collection skip** (a fresh sim controller has empty
  collections for nearly everything and no ZBF defaults — a gate that
  skips on empty is vacuously green).
- **Isolation**: each write scenario gets a fresh site on the seeded
  controller and its own tofu workdir (tmp_path); the provider plugin dir
  is cached once per session. Device scenarios use the sim container's
  `default` site, sharing distinct seeded devices. UOS scenarios use its
  `default` site.

## Scenarios

Write-E2E mechanics: `ubitofu generate` against the scenario site →
`tofu init` + `apply` the imports with the real `ubiquiti-community/unifi`
provider (admin credentials; writes land only on the disposable seeded
controller) → mutate via seeder → run `ubitofu reconcile`/`verify`
in-process through `cli.main()` → assert on report text, file contents,
and exit code.

| # | Fixture | Scenario | Covers |
|---|---|---|---|
| S0 | each | smoke: boot/URL readiness + **version enforcement** — live-reported version (sysinfo; UOS equivalent) must equal the expectation when set. ubitofu runs flavors with independent versioning, so per the contract the expectation is per-flavor: `UNIFI_TEST_EXPECT_VERSION` for the network flavors, `UNIFI_TEST_UOS_EXPECT_VERSION` for UOS | wrong/stale/mistagged image is a failure, not a warning |
| S1 | seeded | adopt → apply → reconcile again, in sync | exit 0 baseline; proves the whole loop |
| S2 | seeded | scalar edit on controller → merged in place | exit 10; surgeon in-place edit |
| S3 | seeded | scalar drift below a multiline collection attr in the committed WLAN block | depth-balance regression |
| S4 | seeded | nested/list edit on controller → complex flag | exit 11 |
| S5 | seeded | S2 + S4 mutations in one run | exit 12 |
| S6a | seeded | applied network/WLAN deleted via API → `deleted`; committed-never-applied but live → `pending` | classify_diverged, both branches |
| S6b | sim | applied `unifi_device` (demo AP) removed via `cmd/sitemgr delete-device` → `deleted` | deleted-device advice, MAC identity |
| S7 | seeded | resource block removed from .tf while still in state | orphaned → exit 11 |
| S8 | seeded | new object created after adoption → appended + import block | exit 10 append path |
| S9 | — | unreachable controller URL (no container; never stops the shared session container) | exit 1 error path |
| S10 | seeded | verify: clean → 0; real drift → 11 | verify outcome codes |
| S11 | uos | enumerate → generate → plan roundtrip via `/proxy/network` + `X-API-KEY` | production-dialect regression |
| S12 | uos | scalar-drift reconcile through the native dialect | contingent on provider apply working against UOS sim |

S6b caveat: device config *updates* through the provider may hang on
provisioning against simulated devices — the scenario is import-only
(adopt, then delete) unless the first round shows updates settle.

## Markers, selection, skip-vs-fail

| Selection | Runs where |
|---|---|
| `-m "not controller"` (default via addopts) | plain `pytest`, existing CI test step, mutmut |
| `-m "controller and not uos"` | Woodpecker controller workflow (URL mode, services) |
| `-m controller` | GHA (dispatch-only for now) and local (testcontainers) |

`controller` marks every container-backed test; `uos` additionally marks
S11/S12. Markers are registered in pyproject; addopts gains
`-m "not controller"` (an explicit CLI `-m` overrides it).

Skip vs fail per the contract: explicitly selected runs with missing
docker or missing URL env get a friendly skip locally, but
**`UNIFI_TEST_REQUIRE`** (always set in CI) turns that skip into a
failure — no skip may satisfy a required check. If testcontainers-python
has an analogue of testcontainers-go's pre-v0.43 hostless-machine panic,
explicit selection must still report a clean failure/skip, not a stack
trace. Permitted skips: documented per-controller-version capability gaps
(endpoint 404) and empty collections with no seedable fixture yet, each
with a tracked follow-up. A non-JSON HTTP 200 from a probe is a failure,
never a skip.

## CI wiring

**Woodpecker (live).** A second workflow file beside the existing one. The
seeded and sim containers run as Woodpecker *services* (plain containers,
no special caps — fine under the rootless-podman agent). The test step
exports `UNIFI_TEST_SEEDED_URL`/`UNIFI_TEST_SIM_URL`,
`UNIFI_TEST_EXPECT_VERSION` (from the pin), and `UNIFI_TEST_REQUIRE`, then
runs `pytest -m "controller and not uos"`; readiness comes from the URL-
mode login poll (Woodpecker does not gate steps on service health). No
docker socket, no testcontainers, no DinD in Woodpecker. Same events as
the existing test step. The mutation gate remains containerless. UOS is
excluded on Woodpecker deliberately: its runtime contract is not
expressible as a service under the rootless agent.

**GHA (present, disabled).** `.github/workflows/controller-tests.yml`,
`workflow_dispatch` only — cron/push triggers commented out and documented
as dormant. On `ubuntu-latest` with real docker, testcontainers runs the
full matrix including UOS-native via `with_kwargs` (mirroring
unifi-containers' `run-uos.sh` contract), with `UNIFI_TEST_REQUIRE` and
`UNIFI_TEST_EXPECT_VERSION` set. The dormant-trigger TODO block records
the enablement mechanics learned in go-unifi: (a) when this becomes a
path-filtered required check it needs an exact-inverse same-name no-op
twin, or non-matching PRs hang forever on "Expected" (see go-unifi
`integration-noop.yaml`); (b) a missing image tag stays honest-red — no
build fallback; (c) enable a nightly scheduled run alongside PR triggers
to catch upstream drift and image rot.

## Empirical questions and decision trees

Resolved by TDD-ing the first scenario of each flavor, not a separate
spike phase:

1. **Provider apply against the seeded container** (S1). Expected to
   work — the provider's own acceptance tests run against demo-mode
   controllers.
2. **Headless API-key mint on UOS sim** (S11): UOS SSO login
   (`/api/auth/login`, admin/admin) then mint via the UOS users API.
   Works → done. Doesn't → preferred fallback is baking a pre-minted key
   into the UOS `-sim` image (small unifi-containers change) rather than
   teaching ubitofu UniFi OS cookie auth for tests' sake.
3. **Provider apply against UOS sim** (S12). Fails → S12 waits; S11's
   plan-level coverage stands alone.
4. **Endpoint coverage per variant vs MANIFEST** — enumerate against each
   flavor once and record which endpoints 404/empty; scenarios only
   depend on endpoints the variant serves, and each gap gets the
   contract's documented-skip treatment with a tracked follow-up.

## Flakiness policy

One container per flavor per session; hard healthy-wait timeouts with the
container log tail dumped on failure. Brief retries only for
eventual-consistency reads after mutations, never around assertions.
Teardown always removes containers and anonymous volumes (reaper
backstop) unless `UNIFI_TEST_KEEP=1`.

## Out of scope / follow-ups

- Version matrix (the 10.0.162 lineage exists in unifi-containers when
  wanted; the pin makes it a one-line change).
- UOS on Woodpecker.
- Enabling the GHA workflow's cron/push/nightly triggers and the
  release-process gate itself (mechanics pre-recorded in the TODO block).
- Sim-write-fidelity characterization beyond what scenarios need.
- Device adoption / inform-protocol emulation (dropped — simulation mode
  supplies devices).

## Sequencing

1. Harness bootstrap (`tests/controllertest/`): dependency group, markers,
   the seeded fixture, URL mode with the normative login poll, S0 smoke +
   version enforcement — just enough to boot a controller in a test.
2. `controller.py` classic dialect (TDD against the live seeded
   container), then the rest of the harness: sim/UOS fixtures, seeder.
3. Scenarios S1 → S3 → S6a/S6b (reconcile-errors core), then
   S2/S4/S5/S7–S10.
4. UOS-native: key-mint bootstrap, S11, S12 if apply works.
5. CI: Woodpecker workflow, dormant GHA workflow.
