# Convergence review: container controller testing design

**From:** the go-unifi controller-testing session (PR #1 on
jamesbraid/go-unifi), 2026-07-19. A cross-implementation comparison of
this spec against go-unifi's now-final harness found the deep
architecture already aligned — image-owned readiness, pinned pulled
images, thin independent seeding clients, structural test selection,
honest-red on missing tags. The items below are the divergences to
resolve, plus decisions already made that this spec should adopt.

**Normative contract (canonical copy):** branch `testing-contract` on
github.com/jamesbraid/unifi-containers → `docs/testing-contract.md`.
Read it first; it is the source of truth both harnesses conform to.

## Decisions already made (adopt as-is)

1. **Module name: `controllertest`** in every language (stdlib
   `httptest` naming pattern; "controller" is Ubiquiti's own wire-level
   product ID — `unifi-controller` in the firmware API and download
   URLs — regardless of marketing's rename). Go's package is
   `internal/controllertest`; name the Python module/fixture package the
   same.
2. **Env vars are `UNIFI_TEST_*`, not `UBITOFU_*`** — the domain is the
   controller, not any one consumer, and the three harnesses must be
   able to share a CI env block. Per-flavor forms:
   `UNIFI_TEST_SEEDED_URL`, `UNIFI_TEST_SIM_URL`, `UNIFI_TEST_UOS_URL`,
   plus `UNIFI_TEST_EXPECT_VERSION`, `UNIFI_TEST_REQUIRE`,
   `UNIFI_TEST_KEEP`. (Spec lines ~75-79 rename accordingly.)

## Spec changes required (mistakes go-unifi already paid for)

3. **Make the URL-mode login-poll criterion normative** (spec ~75-78):
   ready ⇔ the login endpoint returns a **JSON body with `rc == "ok"`**.
   HTTP 200 is never sufficient — during the first ~25s of boot the
   controller answers *every* path, including login, with an HTML
   placeholder page and HTTP 200. Retry connection errors and non-JSON
   bodies; fail fast on JSON `rc != "ok"` or an HTTP error status.
   go-unifi reference: `internal/controllertest/session.go` (Login) and
   `controller.go` (waitReady), with a fast unit test flipping a fake
   server from placeholder to JSON (`controller_test.go`).
4. **Add version enforcement** — the spec pins images but never checks
   the pin took; a stale or mistagged image boots silently and
   everything greens against the wrong build. Each flavor's first smoke
   scenario must assert live-reported version (sysinfo for network; UOS
   equivalent) equals `UNIFI_TEST_EXPECT_VERSION` when set, and CI must
   always set it from the repo's pin.
5. **State the seed-failure rule** (spec ~80-83): a seeder error fails
   the scenario — it must never decay into an empty-collection skip.
   go-unifi's drift gate was vacuously green (all seven resources skip
   on empty collections) until seeding was made mandatory; a fresh sim
   controller has empty collections for everything and no ZBF defaults
   (POST firewall/zone fails `CouldNotFindHotspotFirewallZone`).
6. **Record two GHA mechanics in the dormant workflow's TODO** (spec
   ~144-150): (a) if it ever becomes a required check with path filters,
   it needs an exact-inverse same-name no-op twin or non-matching PRs
   hang forever on "Expected" (see go-unifi
   `.github/workflows/integration-noop.yaml`); (b) missing image tags
   stay honest-red — no build fallback. Also add a scheduled (nightly)
   run when GHA triggers are enabled, per go-unifi's precedent.
7. **Skip-vs-fail knob**: spec ~126-129's "fail loudly when explicitly
   selected" is right — implement it as `UNIFI_TEST_REQUIRE` (set in CI,
   unset locally) so laptops keep a friendly skip while no skip can
   satisfy a CI gate.

## Things this spec has that go-unifi adopted (already done, FYI)

- Bounded URL-mode readiness poll (was missing in Go; the spec's
  CI-services rationale is correct and it is now implemented and
  live-verified against a booting container).
- Container log tail dumped on readiness-timeout.
- `UNIFI_TEST_KEEP` teardown-skip debug knob.

## Useful facts from go-unifi's empirical rounds

- testcontainers-go < v0.43 panics (not errors) constructing a provider
  on hostless machines; if testcontainers-python has an analogous edge,
  make sure explicit selection still reports a clean failure/skip.
- The `-sim` images boot to healthy in ~15-25s (native arch); UOS is
  slower — the spec's 10-minute start period stands.
- Init scripts baked into images must live in `/usr/local/unifi/init.d`,
  never `/unifi/init.d` (`/unifi` is a VOLUME; baked content under it is
  silently discarded) — already handled in unifi-containers, relevant if
  this repo ever derives its own image variants.
