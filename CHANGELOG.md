# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.3.0] - 2026-07-04

### Fixed

- Assign collision-free slugs: seed the reserved set from both tofu state and committed config files so new objects never reuse a slug already in use; guard intra-batch collisions with a running `used` set so suffixed base names cannot collide within the same batch.
- Emit a `variable {}` declaration and a reconcile warning for secret-bearing new objects so plans never fail with an undeclared-variable error.
- Use a unique tempfile for the plan-prelude scratch file instead of writing directly to `imports.tf`; clean up via `try/finally` so a write failure never leaves stale HCL in the working directory.
- Flag resources present in tofu state but absent from committed config as would-be-destroyed rather than silently ignoring them.
- Distinguish controller-deleted objects (present in state, gone from the live controller) from not-yet-applied objects (in config but not in state) with separate report messages.
- Print a one-line error and exit non-zero on known failures (controller unreachable, tofu error, 1Password error) instead of raising an unhandled traceback.

### Added

- `python -m ubitofu` entry point via `__main__.py`.
- `__version__` attribute in `ubitofu.__init__` and this CHANGELOG.
- Precise per-attribute drift flags using `deepdiff`; nested maps, lists, and computed fields each produce a targeted flag instead of a generic "differs" message.
- Hypothesis property-based tests covering HCL surgeon correctness (idempotence, byte-preservation, anchor safety) and slug-assignment invariants (no reserved collisions, no intra-batch collisions).

### Removed

- Dead `--mode` flag from the `reconcile` subcommand.

[0.3.0]: https://github.com/jamesbraid/ubitofu/releases/tag/v0.3.0
