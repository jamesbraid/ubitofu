# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.9.1] - 2026-08-03

### Fixed

- `reconcile` now recognises declared lists, maps, and nested blocks as pending
  HCL intent when the controller still matches the last apply. Check mode no
  longer blocks the apply that would make those changes real. Controller drift
  and concurrent edits still require review. Removing a complex attribute
  remains conservative because no HCL declaration can anchor that intent.

## [0.9.0] - 2026-08-02

### Changed

- **Breaking:** a failure now exits with a code that says what failed, instead
  of everything landing on `1`. `20` cannot reach or use the controller, `21`
  the controller rejected the credentials, `22` the secret could not be read,
  `23` tofu itself failed. `1` now means only what its message always said: an
  error ubitofu did not anticipate. Update any wrapper that reads `rc -eq 1`
  as "something went wrong". One that tests for nonzero is unaffected.

  Branch on them, because the fixes differ. A retry is reasonable on `20` and
  pointless on `21`. `22` means the environment lost its 1Password session,
  not that anything is wrong with the controller. `23` after a provider bump
  is what `ubitofu migrate` explains: the plan fails outright, so `reconcile`
  never gets one to read.
- The controller scenarios no longer hand-roll waits for the test controller to
  finish booting. unifi-containers' images now gate their own readiness on the
  v2 API surface and the full demo fleet as well as a working login, and serve
  that same verdict over HTTP on `:9099/readyz` for callers that did not start
  the container and so cannot read Docker's. Container runs wait on the
  healthcheck as before, now a stronger signal. URL-mode runs poll `/readyz`
  when `UNIFI_TEST_<FLAVOR>_READY` names it, and fall back to the login poll
  otherwise. Four polls came out — two for the demo fleet, two for the v2
  surface — along with the `Seeder.v2_status` probe that fed them.
- Test-target images are pinned by digest as well as tag. Upstream rebuilds a
  published version in place when the image itself changes, and build numbers
  never appear in an image tag, so `10.4.57-sim` alone does not say which build
  it is. testcontainers will not re-pull a tag already in the local cache, so
  without the digest a stale machine keeps running the build whose readiness
  the suite no longer waits for.

### Fixed

- A `tofu fmt` failure is reported as a tofu failure rather than an
  unexpected one. It raised a bare `RuntimeError`, so malformed HCL reached
  the catch-all and told the operator to file a bug about their own config.
- `reconcile` no longer raises attention on data sources. A `data` block is
  read, never created or destroyed, so it has no existence to decide about —
  but it arrives in state and in the plan carrying neither a create nor a
  delete, which 0.8.0's existence classifier read as a state/config invariant
  violation. Every run flagged every data source for manual review. Found on a
  config with three `data "unifi_firewall_zone"` blocks, where the exit code
  did not change but the attention section filled with entries no operator can
  act on.

## [0.8.0] - 2026-08-02

### Added

- `unifi_ap_group` is now enumerated and generated. Custom AP groups become
  `unifi_ap_group` resources with their `device_macs` member list. The
  built-in "All APs" group is controller-managed, implicitly holding every
  AP, so it is skipped and reported as a coverage gap rather than emitted.
- A `uos-seeded` controller flavor: the owner-seeded UniFi OS Server image,
  whose headless login works on 443. That un-xfails the native-dialect
  scenario, which now generates over `/proxy/network` with the
  `X-API-KEY` the image bakes in — the production shape, previously
  unreachable because the `-sim` image cannot complete an SSO login
  headlessly.
- Controller scenarios can run against emulated devices. In container mode the
  controller fixture now puts its container on a Docker network of its own and
  reports the inform URL a container on that network can reach it at.
  `unifi-emu-herder` starts the device fleet there. The harness adopts each MAC
  the herder reports and waits for it to reach connected. The herder is given
  no credentials and does no adoption, so the controller side of that exchange
  lives in `tests/controllertest/adopt.py`.

  These scenarios carry the `herder` marker and are skipped unless
  `UNIFI_TEST_HERDER_BIN` points at a herder binary. It must report the
  version `pins.py` pins (`unifi-emu` 0.5.1), because that binary carries the
  synthetic device image built from the same tag — pinning the version pins
  both halves, and a binary off the pin fails rather than quietly testing a
  different emulator. Get one with
  `go install github.com/jamesbraid/unifi-emu/cmd/unifi-emu-herder@v0.5.1`
  or from the release archive. A binary built from a working tree carries no
  release identity and needs `UNIFI_TEST_HERDER_SYNTHETIC_IMAGE` instead.

  They need a Docker socket, so the Woodpecker workflow excludes them. The
  GitHub workflow installs the pinned release through `unifi-emu`'s own
  `install-herder` action and runs them.

  A controller reached over `UNIFI_TEST_<FLAVOR>_URL` has no container to
  inspect and starts no devices, unless `UNIFI_TEST_<FLAVOR>_NETWORK` and
  `UNIFI_TEST_<FLAVOR>_INFORM_URL` are both set.
- `ubitofu migrate` reads the schema and reports what a provider bump breaks,
  before anything tries to plan. Drop an attribute a config still sets, make
  one required, or make one computed-only, and `tofu plan` fails outright —
  leaving `reconcile` no plan to read. `migrate` compares the installed
  provider's schema against a baseline in
  `<workdir>/.ubitofu/provider-baseline.json`, keeps only what your committed
  HCL can hit, and names the `file:line` of every assignment a removal forces
  you to change. It exits 11 when anything needs attention, 0 when nothing
  does. Run `--write-baseline` once on your current version, then again after
  the bump. It edits nothing: removed attributes are often nested, and the
  surgeon edits only top-level scalars.

  New attributes are reported for review rather than as blockers. The schema
  JSON carries no defaults, so a new attribute that will override a live value
  looks exactly like one that will not. Only a plan against the controller can
  tell them apart, which is the other half of this release.

### Fixed

- The UOS controller scenarios (`pytest -m uos`) start again. Current
  testcontainers versions take no `tmpfs` constructor argument and pass their
  own alongside whatever the caller supplied, so the UOS runtime contract's
  tmpfs set made every boot raise `DockerClient.create() got multiple values
  for keyword argument 'tmpfs'` before the container existed. The mounts now
  go through `with_tmpfs_mount`.
- `reconcile` now treats committed resource blocks as the desired existence
  set. A live object that matches configured HCL but is missing from state gets
  an import for the existing address instead of a duplicate resource block.
  Missing config stays missing: state-only objects follow explicit plan
  destroy or forget actions and are never recreated from state. Replacement,
  ambiguous identity, and unsupported removal plans require review.
- `reconcile` no longer mistakes a provider default for your intent. It
  compared the live controller against the plan's `after` values, which
  already include provider defaults, so an attribute your HCL never mentions
  arrived looking like committed config. When the last-applied state agreed
  with live — the ordinary case after any apply — reconcile read that as an
  unapplied edit and reported nothing, while apply overwrote the controller's
  value. The committed text now decides what the config asks for. If the block
  does not declare an attribute, reconcile writes the live value into it and
  counts it as captured drift (exit 10).

  Found while bumping `ubiquiti-community/unifi` to 0.101.0, which gave
  `unifi_wlan.roaming_assistant_na_enabled` a static `false` default and
  planned the roaming assistant off on every WLAN that had it on. The provider
  fixed that at 0.101.1. Nothing stops the next one.

### Changed

- The controller-scenario suite pins the provider under test
  (`tests/controllertest/pins.py`, override with `UNIFI_TEST_PROVIDER_SOURCE`
  / `_VERSION`). It previously wrote a `source` with no `version`, so every run
  took whatever the registry served that day. This changes no install: default
  `pytest` runs exclude those tests. It does explain how
  `docs/provider-import-bugs.md` came to cite a version the suite never ran
  against. Both bugs recorded there survive a retest on the pinned provider,
  so the write scenarios stay parked.

## [0.7.2] - 2026-08-02

### Fixed

- A committed value that references a resource the same apply creates is no
  longer reported as drift. It is unknown at plan time, so tofu writes null
  into the change's `after` and records the path in `after_unknown`, and the
  null is then dropped as empty — leaving the attribute looking absent from
  config. `reconcile` flagged that for manual review, which set the attention
  outcome and returned exit 11. An apply gate that blocks on 11 could never
  clear it: 11 means nothing is capturable, so the reconcile it points at
  opens no PR, and the apply that would settle the reference is the thing
  being blocked. Reconcile now reads `after_unknown` and treats those paths as
  pending. Suppression is per path, so real drift beside an unknown sibling
  still flags.

## [0.7.1] - 2026-08-01

### Fixed

- `reconcile` and `reconcile --check` no longer abort on sites with a PoE power
  supervisor. The manifest keyed `unifi_power_supervisor` by `mac`, but the v2
  record has no `mac` field: identity is `id`, and the supervised device's MAC is
  `client_mac`. Identity derivation raised `ValueError` and took the whole run
  down with it, including the apply gate that shares the same ingestion.
  Supervisors are now counted as a coverage gap rather than adopted. Expect a
  `N device power supervisor(s)` line where the crash used to be. The spec
  records the correct `_id` rule for whenever adoption is un-parked.
- The per-PR mutation gate no longer dies collecting stats. `mutmut` runs the
  suite from `mutants/`, which gets only the source and test trees, so the
  pin-drift tripwire's raw-text reads of `.woodpecker/` and `.github/` hit
  missing files. `also_copy` now carries both across.

## [0.7.0] - 2026-07-22

### Added

- Self-hosted standalone controllers: set `dialect = "classic"` in the config
  for controllers that use cookie login instead of an API key, with
  `username`, `password_source` (`env` or `op`), and `password_ref` supplying
  the credentials. UniFi OS consoles (UDM, Cloud Key) keep the default
  dialect and need no config change.
- Live controller-scenario tests under `tests/controllertest`, marked
  `controller` (plus `uos` for the UniFi OS Server flavor) and excluded from
  default `pytest` runs. They boot pinned unifi-containers images
  (`ghcr.io/jamesbraid/unifi-network`, `ghcr.io/jamesbraid/unifi-os-server`)
  via testcontainers, or target an already-running controller through the
  `UNIFI_TEST_*` environment contract (`UNIFI_TEST_<FLAVOR>_URL` / `_IMAGE`,
  `UNIFI_TEST_EXPECT_VERSION`, `UNIFI_TEST_REQUIRE`).
- Write-path scenarios (generate → apply round-trips) are parked on two
  `ubiquiti-community/unifi` provider import bugs;
  `docs/provider-import-bugs.md` records the evidence and the un-parking
  checklist.

### Changed

- An incomplete or inconsistent config file now exits `2` with
  `ubitofu: config error: ...` naming the missing keys (for example a
  classic dialect without `password_source`, or an `op` source without
  `op_vault`), instead of a traceback or the generic "unexpected error"
  report. Validation runs after CLI flag overrides, so flags can still
  complete a partial config file.
- A controller that rejects the credentials (HTTP 401/403) is now
  reported as `authentication failed` — previously it was
  indistinguishable from `cannot reach the UniFi controller`. Both still
  exit `1`.

## [0.6.1] - 2026-07-19

### Fixed

- Staged deletions now name every config site that still references
  the deleted resource (file:line) and hold the attention exit code
  until the drift PR resolves them — previously the deletion and the
  resulting dangling-reference validate failure were disconnected.

## [0.6.0] - 2026-07-19

### Added

- `reconcile --check`: classify and report exactly as a wet run, but write
  nothing to the tree — the check the apply gate reads, for CI that must branch on
  the outcome without ever mutating committed config.
- Exit `13`: a planned `unifi_device` create is now caught during reconcile
  and reported by address, ahead of every other outcome — adoption is
  UI-only, so no apply may create a device.

### Changed

- reconcile is now three-way: committed config, live controller, and the
  last-applied state are all consulted, so controller drift and unapplied
  local intent are told apart instead of conflated.
- Resources deleted on the controller are no longer just flagged — their
  committed blocks are staged for removal in the working tree, so the PR
  diff itself is the review surface.
- Objects present live and in state but never committed to config (orphans)
  are now codified into config instead of being left for the next apply to
  destroy.

## [0.5.0] - 2026-07-19

### Added

- **Breaking:** every subcommand now signals its outcome through the exit
  code (see `--help` or the README). Errors exit `1`, no longer `2`; `2` now
  means usage error only. Update any wrapper that treats a nonzero exit as
  failure.

### Fixed

- hcl_surgeon: multiline list and object values no longer derail brace-depth
  tracking, which hid every later top-level scalar from in-place edits.
  After adding support for AP groups, `unifi_wlan` blocks start with a
  multiline `ap_group_ids` list, so their scalar drift never auto-merged;
  now it does.
- reconcile: a committed resource whose controller object was deleted is now
  flagged "deleted on controller — remove from config or re-adopt" rather
  than "not yet applied — run apply". Both cases plan identically, so
  reconcile now consults the live controller to tell them apart — apply
  cannot re-create devices, only manual adoption in the UI can.
- A relative `workdir` in the config (including the default `"."` when running
  from another directory) no longer breaks `generate`/`reconcile`/`verify`.
  Tofu runs with the workdir as its cwd while output paths are passed
  workdir-prefixed, so a relative value made tofu resolve them from inside the
  workdir (`./work` -> `work/work/tf.plan`: "Failed to write plan file").
  `Config` now resolves `workdir` to an absolute path on construction.

## [0.4.0] - 2026-07-11

### Added

- Schema-driven coverage audit: every live setting section/field, probed
  endpoint, and provider resource is checked against
  `tofu providers schema -json`; findings render byte-stably into
  `COVERAGE.md` (written by reconcile/generate) and the console report.
- No silent ignoring: a git merge of the COVERAGE.md diff is the only
  acceptance mechanism; the only code-level classification is `super_*`
  (console-scope).
- Manifest-lag check: flags provider resources with no MANIFEST mapping
  (currently `unifi_ap_group`).

### Breaking Changes

- `enumerate` now requires a tofu-init'd workdir (provider schema
  is mandatory — no degraded mode).

### Removed

- `UNMAPPED_ENDPOINTS`, `_guest_network_gaps` (both folded into
  the audit; guest networks still reported, by `coverage.audit_guest_networks`).

## [0.3.3] - 2026-07-05

### Fixed

- `reconcile` now derives a resource's identity the same way from the controller
  and from tofu state, so no resource type can be silently re-added as a
  duplicate. Both sides delegate to a single `derive_identity` function; drift
  between the two implementations is structurally impossible. Fixes the latent
  `unifi_setting` / `unifi_bgp` singleton case and prevents the whole class of
  asymmetry bug (the WireGuard peer duplicate was the first instance; this is
  the systematic fix).

## [0.3.1] - 2026-07-05

### Fixed

- `reconcile` no longer re-adds already-managed WireGuard peers as duplicates on
  every run. Their identity is now reconstructed as `network_id:peer_id` to match
  how the enumerator records them, so managed peers are recognised and skipped
  rather than appended as `example_peer_2`, `example_peer_3`, and so on.

## [0.3.0] - 2026-07-04

### Added

- Sharper `reconcile` reporting: it names each changed nested attribute, flags resources
  that would be destroyed on apply, distinguishes controller-deleted objects from
  unapplied ones, and emits the `variable` declaration for new secret-bearing objects so
  a plan no longer fails on an undeclared variable.
- `python -m ubitofu`, an `ubitofu.__version__` attribute, and a Claude Code workflow
  skill (`unifi-tofu-reconcile-workflow`; see the README).

### Fixed

- `reconcile` is safe to re-run: it never reuses an existing resource's name, leaves your
  `imports.tf` untouched, and is a clean no-op against an unchanged controller.
- Known failures (controller unreachable, tofu error, 1Password not signed in) print one
  line and exit non-zero instead of a traceback.

### Removed

- Unused `--mode` flag from `reconcile`.

## [0.2.1] - 2026-07-04

### Changed

- Minimum Python lowered to 3.11 for broader compatibility with common CI images.

## [0.2.0] - 2026-07-04

### Added

- `reconcile` command: merges live controller drift back into committed HCL in place,
  preserving comments and layout, instead of regenerating wholesale.
- Project branding: mascot, logo, and icons.

## [0.1.0] - 2026-07-02

### Added

- Initial release: `enumerate`, `generate`, and `verify` commands to bring a live
  UniFi/UDM controller under OpenTofu management, generating clean, directly-appliable
  HCL for the `ubiquiti-community/unifi` provider. Plan-only and re-runnable. Plaintext
  secrets are never written to files.

[Unreleased]: https://github.com/jamesbraid/ubitofu/compare/v0.9.1...HEAD
[0.9.1]: https://github.com/jamesbraid/ubitofu/compare/v0.9.0...v0.9.1
[0.9.0]: https://github.com/jamesbraid/ubitofu/compare/v0.8.0...v0.9.0
[0.8.0]: https://github.com/jamesbraid/ubitofu/compare/v0.7.2...v0.8.0
[0.7.2]: https://github.com/jamesbraid/ubitofu/compare/v0.7.1...v0.7.2
[0.7.1]: https://github.com/jamesbraid/ubitofu/compare/v0.7.0...v0.7.1
[0.7.0]: https://github.com/jamesbraid/ubitofu/compare/v0.6.1...v0.7.0
[0.6.1]: https://github.com/jamesbraid/ubitofu/compare/v0.6.0...v0.6.1
[0.6.0]: https://github.com/jamesbraid/ubitofu/compare/v0.5.0...v0.6.0
[0.5.0]: https://github.com/jamesbraid/ubitofu/compare/v0.4.0...v0.5.0
[0.4.0]: https://github.com/jamesbraid/ubitofu/compare/v0.3.3...v0.4.0
[0.3.3]: https://github.com/jamesbraid/ubitofu/compare/v0.3.1...v0.3.3
[0.3.1]: https://github.com/jamesbraid/ubitofu/compare/v0.3.0...v0.3.1
[0.3.0]: https://github.com/jamesbraid/ubitofu/releases/tag/v0.3.0
[0.2.1]: https://github.com/jamesbraid/ubitofu/releases/tag/v0.2.1
[0.2.0]: https://github.com/jamesbraid/ubitofu/releases/tag/v0.2.0
[0.1.0]: https://github.com/jamesbraid/ubitofu/releases/tag/v0.1.0
