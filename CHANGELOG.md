# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
  rather than appended as `sputnik_2`, `sputnik_3`, and so on.

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

[Unreleased]: https://github.com/jamesbraid/ubitofu/compare/v0.3.3...HEAD
[0.3.3]: https://github.com/jamesbraid/ubitofu/compare/v0.3.1...v0.3.3
[0.3.1]: https://github.com/jamesbraid/ubitofu/compare/v0.3.0...v0.3.1
[0.3.0]: https://github.com/jamesbraid/ubitofu/releases/tag/v0.3.0
[0.2.1]: https://github.com/jamesbraid/ubitofu/releases/tag/v0.2.1
[0.2.0]: https://github.com/jamesbraid/ubitofu/releases/tag/v0.2.0
[0.1.0]: https://github.com/jamesbraid/ubitofu/releases/tag/v0.1.0
