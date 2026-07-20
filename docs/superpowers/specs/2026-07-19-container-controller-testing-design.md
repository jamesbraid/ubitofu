# Container-controller integration testing — Design

Date: 2026-07-19
Status: approved

## Mission

Live-controller integration coverage for the reconcile-errors work shipped in
v0.5.0, plus the harness every future scenario builds on. The behaviors under
test:

1. **hcl_surgeon depth-balance fix** — scalar drift below a multiline
   collection value must still merge (pre-fix, every later top-level attr
   became invisible to the scanner).
2. **classify_diverged** — controller-deleted resources classify as
   `deleted`, committed-but-never-applied ones as `pending`, via live
   identities and state identities.
3. **rsync-style outcome exit codes** — 0 in sync, 10 drift captured, 11
   attention required, 12 both, 1 error — across reconcile and verify.

Today's suite covers all of this with fixtures and fakes. This work adds the
missing layer: real controllers, the real provider, real `tofu apply`, real
API mutations — end to end.

## Test targets

All images come from `ghcr.io/jamesbraid/unifi-containers` — version-pinned,
multi-arch, healthcheck-means-API-answers, published (pulled, never built
here). Versions are pinned in one constant each, env-overridable.

| Fixture | Image | Boot | Credentials | Role |
|---|---|---|---|---|
| `seeded_controller` | `ghcr.io/jamesbraid/unifi-network:10.4.57-seeded` | ~15 s | `admin` / `unifi-containers-seeded` | Write-E2E scenarios. Real empty site, wizard pre-completed at image build. |
| `sim_controller` | `ghcr.io/jamesbraid/unifi-network:10.4.57-sim` | ~30–40 s | `admin` / `admin` | Device scenarios. Simulation mode seeds 3 APs, 1 gateway, 5 switches with real MACs. |
| `uos_controller` | `ghcr.io/jamesbraid/unifi-os-server:5.1.21-sim` | 1–5 min | `admin` / `admin` | UOS-native dialect regression: `/proxy/network` + `X-API-KEY` on port 443 — ubitofu's production dialect. |

Write scenarios stay off the sim variants: per the unifi-containers README,
sim is a controller-API test double, and write fidelity is a per-version
empirical question. The seeded image is a real empty controller, so writes
there mean what they mean in production.

The UOS container requires the documented runtime contract (cap list, host
cgroupns with `/sys/fs/cgroup` rw, tmpfs set) — the canonical form is
`unifi-os/examples/docker-compose.yml` in unifi-containers; the harness
passes it via testcontainers `with_kwargs`.

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

This closes the self-hosted gap the README already claims to support. It is
a prerequisite for every classic-image scenario, so it lands first.

## Harness

- **testcontainers-python** (`testcontainers>=4.14`) in a new optional
  dependency group `controller` (not `dev` — mutation and lint envs never
  need docker deps).
- Session-scoped fixtures per flavor, `DockerContainer` +
  `HealthcheckWaitStrategy` with generous startup timeouts (first pull +
  boot; UOS healthcheck start period is 10 min). Ryuk reaps containers even
  on hard kills.
- **URL mode**: `UBITOFU_TEST_SEEDED_URL`, `UBITOFU_TEST_SIM_URL`,
  `UBITOFU_TEST_UOS_URL` bypass container boot and target an existing
  controller (this is how Woodpecker services plug in). In URL mode the
  fixture login-polls for readiness itself.
- `UBITOFU_KEEP_CONTROLLER=1` skips teardown for debugging.
- `seeder.py`: thin httpx client (cookie login) that creates/mutates/deletes
  controller objects — networks, WLANs, users, sites — to build scenarios,
  plus `cmd/sitemgr` helpers (`add-site`, `delete-device`).
- **Isolation**: each write scenario gets a fresh site on the seeded
  controller and its own tofu workdir (tmp_path); the provider plugin dir is
  cached once per session. Device scenarios use the sim container's
  `default` site, sharing distinct seeded devices. UOS scenarios use its
  `default` site.

## Scenarios

Write-E2E mechanics: `ubitofu generate` against the scenario site →
`tofu init` + `apply` the imports with the real `ubiquiti-community/unifi`
provider (admin credentials; writes land only on the disposable controller)
→ mutate via seeder → run `ubitofu reconcile`/`verify` in-process through
`cli.main()` → assert on report text, file contents, and exit code.

| # | Fixture | Scenario | Covers |
|---|---|---|---|
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
(adopt, then delete) unless the spike shows updates settle.

## Markers and selection

| Selection | Runs where |
|---|---|
| `-m "not controller"` (default via addopts) | plain `pytest`, existing CI test step, mutmut |
| `-m "controller and not uos"` | Woodpecker controller workflow (URL mode, services) |
| `-m controller` | GHA (dispatch-only for now) and local (testcontainers) |

`controller` marks every container-backed test; `uos` additionally marks
S11/S12. Markers are registered in pyproject; addopts gains
`-m "not controller"` (an explicit CLI `-m` overrides it). When explicitly
selected without docker or the needed URL env, tests fail loudly — no
silent skips reading as green.

## CI wiring

**Woodpecker (live).** A second workflow file beside the existing one. The
seeded and sim containers run as Woodpecker *services* (plain containers, no
special caps — fine under the rootless-podman agent; readiness comes from
the fixtures' URL-mode login poll, since Woodpecker does not gate steps on
service health). The test step exports the two URL-mode env vars and runs
`pytest -m "controller and not uos"`. No docker socket, no testcontainers,
no DinD in Woodpecker. Same events as the existing test step (push,
pull_request, tag, manual). The mutation gate remains containerless. UOS is
excluded on Woodpecker deliberately: its runtime contract (caps, cgroupns,
tmpfs) is not expressible as a service under the rootless agent.

**GHA (present, disabled).** `.github/workflows/controller-tests.yml`,
`workflow_dispatch` only — cron/push triggers commented out and documented
as dormant (house style: unifi-containers' shadow updaters). On
`ubuntu-latest` with real docker, testcontainers runs the full matrix
including UOS-native via `with_kwargs` (mirroring unifi-containers'
`run-uos.sh` contract). A marked TODO block records where the
release-process hook lands when releases wire through GHA.

## Empirical questions and decision trees

Resolved by TDD-ing the first scenario of each flavor, not a separate spike
phase:

1. **Provider apply against the seeded container** (S1). Expected to work —
   the provider's own acceptance tests run against demo-mode controllers.
2. **Headless API-key mint on UOS sim** (S11): UOS SSO login
   (`/api/auth/login`, admin/admin) then mint via the UOS users API. Works →
   done. Doesn't → preferred fallback is baking a pre-minted key into the
   UOS `-sim` image (small unifi-containers change) rather than teaching
   ubitofu UniFi OS cookie auth for tests' sake.
3. **Provider apply against UOS sim** (S12). Fails → S12 waits; S11's
   plan-level coverage stands alone.
4. **Endpoint coverage per variant vs MANIFEST** — enumerate against each
   flavor once and record which endpoints 404/empty; scenarios only depend
   on endpoints the variant serves.

## Flakiness policy

One container boot per flavor per session; hard healthy-wait timeouts with
container logs dumped on failure. Brief retries only for
eventual-consistency reads after mutations, never around assertions.
Teardown always removes containers (Ryuk backstop) unless
`UBITOFU_KEEP_CONTROLLER=1`.

## Out of scope / follow-ups

- Version matrix (the 10.0.162 lineage exists in unifi-containers when
  wanted; the pin makes it a one-line change).
- UOS on Woodpecker.
- Enabling the GHA workflow's cron/push triggers and the release-process
  gate itself.
- Sim-write-fidelity characterization beyond what scenarios need.
- Device adoption / inform-protocol emulation (dropped — simulation mode
  supplies devices).

## Sequencing

1. Harness bootstrap: dependency group, markers, the seeded fixture, URL
   mode — just enough to boot a controller in a test.
2. `controller.py` classic dialect (TDD against the live seeded container),
   then the rest of the harness: sim/UOS fixtures, seeder.
3. Scenarios S1 → S3 → S6a/S6b (reconcile-errors core), then S2/S4/S5/S7–S10.
4. UOS-native: key-mint bootstrap, S11, S12 if apply works.
5. CI: Woodpecker workflow, dormant GHA workflow.
