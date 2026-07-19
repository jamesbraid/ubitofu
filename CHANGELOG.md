# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.6.1] - 2026-07-19

### Fixed

- Staged deletions now name every config site that still references
  the deleted resource (file:line) and hold the attention exit code
  until the drift PR resolves them — previously the deletion and the
  resulting dangling-reference validate failure were disconnected.

## [0.6.0] - 2026-07-19

### Added

- `reconcile --check`: classify and report exactly as a wet run, but write
  nothing to the tree — the apply gate's oracle, for CI that must branch on
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

[Unreleased]: https://github.com/jamesbraid/ubitofu/compare/v0.6.1...HEAD
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
