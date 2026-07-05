# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.3.0] - 2026-07-04

### Fixed

- `reconcile` no longer reuses an existing resource name when adopting a new controller
  object. It checks names already in state and config before choosing a slug, so it
  cannot point tofu at the wrong device.
- Re-running `reconcile` against an unchanged controller is a clean no-op. Previously,
  an already-captured but unapplied object could be re-adopted under a new name on each
  run.
- Known failures (controller unreachable, a tofu error, 1Password not signed in) now
  print a single line and exit non-zero instead of raising a traceback.

### Added

- `reconcile` names each changed nested attribute individually in its report, rather than
  logging a generic "differs".
- When `reconcile` adopts a new object that carries a secret (such as a WLAN passphrase),
  it emits the matching `variable` declaration and warns you to set it, so `tofu plan`
  does not fail on an undeclared variable. No secret value is written to a file.
- `reconcile` flags resources present in tofu state but absent from your config as
  "would be destroyed on apply".
- `reconcile` distinguishes an object deleted on the controller from one present in
  config but not yet applied.
- `python -m ubitofu` as an alternative to the `ubitofu` console script.
- `ubitofu.__version__` attribute and this changelog.
- A Claude Code workflow skill (`unifi-tofu-reconcile-workflow`): see the README.

### Changed

- `reconcile` no longer writes to `imports.tf`.

### Removed

- Unused `--mode` flag from the `reconcile` subcommand.

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

[Unreleased]: https://github.com/jamesbraid/ubitofu/compare/v0.3.0...HEAD
[0.3.0]: https://github.com/jamesbraid/ubitofu/releases/tag/v0.3.0
[0.2.1]: https://github.com/jamesbraid/ubitofu/releases/tag/v0.2.1
[0.2.0]: https://github.com/jamesbraid/ubitofu/releases/tag/v0.2.0
[0.1.0]: https://github.com/jamesbraid/ubitofu/releases/tag/v0.1.0
