<p align="center">
  <img src="https://raw.githubusercontent.com/jamesbraid/ubitofu/main/assets/logo.png"
       alt="ubitofu mascot — a tofu block wearing a UniFi access point, holding an OpenTofu gear"
       width="220">
</p>

# ubitofu — generate and reconcile UniFi OpenTofu HCL

ubitofu turns a live UniFi Network controller into reviewable HCL for the
`ubiquiti-community/unifi` provider. An administrator can make changes in the
UniFi web UI or mobile app, while an infrastructure repository can make other
changes in HCL. ubitofu captures independent controller changes and blocks when
the controller and HCL changed the same comparable value differently.

The tool is deliberately plan-only. It reads the controller, OpenTofu plans,
and the local module, then writes HCL and structured results. It never writes
the controller, runs `tofu apply`, changes state, manages Git, or owns CI and
deployment policy.

## Installation

```console
pip install ubitofu
```

ubitofu 0.10 supports CPython 3.11 through 3.14 on macOS and Linux. Windows is
unsupported. The tested OpenTofu line is 1.12.x.

Both entry points expose the same commands:

```console
ubitofu --help
python -m ubitofu --help
```

## Configuration

Every command takes `--config`. A UniFi OS controller using an environment
variable for its API key can use:

```toml
controller_url = "https://controller.example.test"
site = "default"
api_key_source = "env"
api_key_ref = "UNIFI_API_KEY"
workdir = "./network"
```

TLS certificate verification is enabled by default. To trust a private CA,
keep verification enabled and name its bundle:

```toml
verify_tls = true
ca_bundle = "/etc/ssl/private-controller-ca.pem"
```

Disabling verification is an explicit local configuration choice. A custom CA
cannot be combined with disabled verification.

Self-hosted classic controllers use cookie login:

```toml
controller_url = "https://controller.example.test"
site = "default"
workdir = "./network"
dialect = "classic"
username = "admin"
password_source = "env"
password_ref = "UNIFI_PASSWORD"
```

Credential sources may be `env` or `op`. With `op`, set `op_vault` and use a
1Password reference for the corresponding `*_ref` setting.

## Command surface

Version 0.10 exposes seven operations:

```text
ubitofu generate
ubitofu reconcile
ubitofu reconcile --dry-run
ubitofu check --plan PLAN
ubitofu inspect
ubitofu health snapshot
ubitofu health compare --before RECEIPT
```

Pass `--config CONFIG` to each operation. `--format human|json` selects output,
and `--output PATH` writes it to a private file instead of standard output.

### Generate an initial module

Configure the object in the UniFi UI or mobile app first, initialize the
OpenTofu root, then run:

```console
ubitofu generate --config config.toml
```

Generation asks OpenTofu for provider-shaped configuration and commits one
validated candidate set. It writes ubitofu-owned resources, import blocks,
sensitive variable declarations, and `COVERAGE.md`. Existing operator content
or ambiguous ownership blocks the whole operation rather than being replaced.

Do not run another OpenTofu process, Git automation, or a file watcher against
the same root while generation is active. OpenTofu requires a short-lived
import scaffold in the root during initial generation. ubitofu removes the
exact scaffold before it interprets plan output and recovers only residue it
can identify exactly.

### Reconcile UI and HCL changes

Preview controller changes without writing HCL:

```console
ubitofu reconcile --dry-run --config config.toml
```

`--dry-run` is a first-class safety interface. It collects the same snapshot
and computes the same decisions, candidate bytes, changed paths, and candidate
digests as a wet reconcile given equivalent inputs. It does not start an HCL
transaction or clean old transaction residue.

Review the decisions and proposed paths, then run the wet command against the
same intended inputs:

```console
ubitofu reconcile --config config.toml
```

The planner compares the last managed value, evaluated HCL intent, and the live
controller value. Its rules are:

- a controller-only change is captured in HCL
- an HCL-only change is preserved
- identical changes on both sides are treated as converged
- different changes to the same comparable value block every write
- independent changes merge in one candidate set.

Nested values are merged only where the public manifest defines a stable
element identity. Other complex values are atomic. A non-literal HCL expression
whose source ownership cannot be proved produces typed
`source_ownership_ambiguous` attention. JSON HCL files (`.tf.json` and
`.tofu.json`) are indexed but read-only in 0.10, so a required JSON edit blocks.

Source files must be regular files owned by the worktree owner. Unsupported
ACLs, extended attributes, file flags, symlinks, stale identities, and unsafe
paths fail closed. Diagnostics name the affected relative path and the metadata
fact when the operating system makes it available.

### Check a saved plan

Create a private saved plan, then check that exact file immediately before an
external workflow applies it:

```console
tofu plan -out=network.tfplan
ubitofu check --plan network.tfplan --config config.toml \
  --format json --output check-receipt.json
```

`check` never creates a replacement plan and never writes HCL. It compares the
plan-time controller view with one fresh controller collection and reports a
digest for the supplied plan. The caller remains responsible for backing up
state, verifying that digest again, applying the same file, and removing the
private artifacts.

Without a controller revision token, a value can still change after its
endpoint was read. Secret-bearing resources have an additional limitation:
`secret_freshness_unverified` warns that ubitofu could not revalidate a
post-plan UI secret edit. The warning exits 0 and does not make overwriting that
secret safe. The external apply workflow must decide whether to continue.

### Inspect coverage and compare health

`inspect` reports controller/provider coverage without writing HCL:

```console
ubitofu inspect --config config.toml
```

Health commands capture a private baseline and compare a later observation:

```console
ubitofu health snapshot --config config.toml \
  --format json --output before-health.json
ubitofu health compare --before before-health.json --config config.toml
```

A new degradation, a newly unknown state, a missing subsystem, or an unavailable
baseline blocks. An unchanged unknown baseline is advisory.

## Results and exit codes

Human and JSON output are projections of the same typed outcome. Human preview
output lists decisions and changed relative paths. JSON preview output carries
the same sorted paths and candidate digests without HCL or secret values.

`--output PATH` creates or atomically replaces an owner-owned regular file at
mode `0600`. It refuses source HCL, `COVERAGE.md`, `.ubitofu` control data, the
generation scaffold, the selected config, and the command's saved-plan or
health-baseline input.

Every command uses the same exit scheme:

| Code | Meaning |
| ---: | --- |
| 0 | success, allowed plan, or advisory warning |
| 1 | operational failure |
| 2 | command-line usage or configuration error |
| 3 | valid blocking outcome: conflict, unsafe plan, or health degradation |

Exit 0 does not mean HCL changed. An external workflow should inspect the
result and the ordinary source diff. Exit 3 is a valid, fully reported outcome,
not a transport or parser failure.

## Secrets

ubitofu never captures a secret value from the controller into HCL, output, a
receipt, or a digest. Explicit top-level secret rules render sensitive
`var.<name>` references. Provider-declared nested sensitive or write-only values
are suppressed and covered by lifecycle ignore policy rather than emitted.

Secret decisions retain only the path and a typed fact:

- an HCL-only secret rotation remains allowed
- a detectable controller-only secret change blocks because it cannot be
  captured
- different detectable changes on both sides block as a secret conflict
- equal detectable changes converge
- an unavailable or write-only value is reported as noncomparable.

Supply variable values through a secret manager, `TF_VAR_*`, or another
OpenTofu mechanism outside this repository.

## Product boundary

ubitofu owns controller discovery, provider-aware generation, three-way
reconciliation, structural HCL edits, saved-plan safety decisions, health
interpretation, and human or JSON results.

It does not own controller writes, OpenTofu apply, state mutation or backup,
deployment credentials, Git commits and pull requests, CI-vendor behavior,
scheduling, or notifications. Those stay in small external tools and workflows.

## Development and testing

See [docs/testing.md](docs/testing.md) for supported runtimes, local checks,
controller scenarios, package verification, and the server-only mutation gate.

## Claude Code workflow skill

The repo includes `.claude/skills/unifi-tofu-reconcile-workflow/` for work on a
UniFi infrastructure repository. It teaches the UI/mobile and HCL coexistence
workflow, including mandatory dry-run review before wet reconcile.

`pip install ubitofu` does not install the skill. Copy the directory into the
consumer repository's `.claude/skills/` directory and commit it there, or copy
it into `~/.claude/skills/` for personal use.

## License

Licensed under GPL-3.0-or-later — see [LICENSE](LICENSE).
