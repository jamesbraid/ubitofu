<p align="center">
  <img src="https://raw.githubusercontent.com/jamesbraid/ubitofu/main/assets/logo.png"
       alt="ubitofu mascot — a tofu block wearing a UniFi access point, holding an OpenTofu gear"
       width="220">
</p>

# ubitofu — import your UniFi / Ubiquiti UDM config into OpenTofu (Terraform)

ubitofu enumerates a live UniFi Network controller (UDM, UDM-Pro, Cloud Key, or
self-hosted) and generates clean, directly-appliable OpenTofu/Terraform HCL for the
`ubiquiti-community/unifi` provider — bringing existing networks, VLANs, WLANs,
firewall rules, port profiles, port forwards, WireGuard VPN, clients and devices under
infrastructure-as-code. Plan-only and re-runnable: run once to import an existing
controller, and again to reconcile drift.

It never runs `tofu apply` and never writes to the controller. Every run reads from the
controller and writes HCL to your working directory, so running it against production
networks is safe.

## Installation

```
pip install ubitofu
```

Requires Python 3.11 or later. Runtime dependencies (python-hcl2, httpx, deepdiff)
install automatically.

Both entry points are equivalent:

```
ubitofu --help
python -m ubitofu --help
```

## Importing an existing UniFi controller into Terraform/OpenTofu

Four subcommands take you from a live controller to appliable code:

```console
$ ubitofu enumerate --config config.toml   # import blocks + coverage gaps (requires tofu-init'd workdir)
$ ubitofu generate  --config config.toml   # imports.tf + generated.tf + unifi-variables.tf
$ ubitofu reconcile --config config.toml   # merge drift into committed HCL in place
$ ubitofu reconcile --check --config config.toml   # gate: classify only, write nothing, same exit codes
$ ubitofu verify    --config config.toml   # plan must be clean (or secrets-only)
```

- `enumerate` walks the controller and prints `import` blocks plus a report of anything
  it cannot bring under management. Requires a tofu-init'd `workdir` — it reads the
  provider schema for the coverage audit.
- `generate` writes `imports.tf`, `generated.tf`, and `unifi-variables.tf` — a
  self-contained, appliable configuration for the `ubiquiti-community/unifi` provider.
- `reconcile` edits your committed, hand-tuned `.tf` in place, preserving comments and
  layout: it updates drifted scalars and names each changed nested attribute precisely
  in the report; appends newly-adopted objects with their `import` blocks (and, for
  objects that carry a secret, the matching `variable` declaration plus a warning to
  set it); flags resources that would be destroyed on apply; and distinguishes objects
  deleted on the controller from those configured but not yet applied. Re-runs against
  an unchanged controller are a no-op and never produce a duplicate resource name. The
  printed report is the product; nothing is applied.
- `verify` runs a plan and passes only when it is clean (or the only diffs are in
  schema-sensitive attributes whose values live in variables).

### Exit codes

Every subcommand uses the same flat, rsync-style scheme — distinct small codes
you can `case` on, no report-grepping:

| code | meaning |
|-----:|---|
| 0    | success — in sync / clean plan / nothing to report |
| 10   | drift captured — committed `*.tf` edited or `reconciled_new.tf` appended (`reconcile`) |
| 11   | attention required — complex/diverged/orphaned/secret findings (`reconcile`), real drift (`verify`) |
| 12   | drift captured AND attention required |
| 13   | forbidden device create — remove the block or adopt via UI (`reconcile`) |
| 1    | error — controller unreachable, tofu failure, secrets |
| 2    | usage error |

Under `set -e`/`pipefail`, capture the code instead of aborting:

```bash
rc=0; ubitofu reconcile --config config.toml | tee report.txt || rc=$?
case "$rc" in
  0)  ;;                                          # nothing to do
  10) open_pr ;;                                  # drift captured
  11) notify "manual attention needed" ;;
  12) open_pr; notify "manual attention needed" ;;
  13) die "device create planned — remove the block or adopt in the UI" ;;
  *)  die "reconcile failed ($rc)" ;;
esac
```

Configuration is TOML:

```toml
controller_url = "https://192.168.1.1"
site           = "default"
api_key_source = "op"                                # or "env"
api_key_ref    = "op://YourVault/unifi.api-key/credential"
op_vault       = "YourVault"
workdir        = "./work"
```

Self-hosted standalone controllers use `dialect = "classic"` (cookie login
instead of an API key); UniFi OS consoles keep the default:

```toml
dialect         = "classic"
username        = "admin"
password_source = "env"                              # or "op"
password_ref    = "UNIFI_PASSWORD"
```

## Coverage audit — nothing is silently ignored

Every run audits the live controller against the provider's schema
(`tofu providers schema -json`): setting sections and their fields, probed
API collections, and provider resources missing from ubitofu's own manifest.
Findings land in two places:

- the console report (`Coverage gaps:` section), and
- `COVERAGE.md` in the workdir — byte-stable, committed alongside your HCL.

`COVERAGE.md` is the acceptance ledger. A new gap arrives as a git diff and
rides whatever drift-PR automation you run; merging that diff is the
acknowledgment. A gap disappears only when a provider release actually
models the config. There are no ignore lists.

## Secrets

Secret attributes (WLAN passphrases, dynamic-DNS passwords, …) are never emitted as
plaintext. Each one known to the `SECRETS` table renders as a `var.<name>` reference,
and `generate` writes a `unifi-variables.tf` declaring every referenced variable
(`type = string`, `sensitive = true`) so the generated config is self-contained.

You supply variable **values** from your secret manager — e.g. `TF_VAR_<name>`
environment variables or a git-ignored `*.auto.tfvars`. The tool prints a suggested
secret-manager reference for each variable (rendered with your configured `op_vault`);
these references are reporter output only — the tool never writes them to files.

ubitofu omits sensitive attributes without a `SECRETS` rule from the HCL and adds them
to `lifecycle { ignore_changes }`. As a safety net, any emitted string value that still
looks secret-shaped (a secret-bearing attribute name, or a 44-char base64 WireGuard-key
shape) is suppressed the same way, with a loud warning naming the resource and
attribute — add a `SECRETS` rule to manage it properly.

## How it compares

Unlike general-purpose importers such as `terraformer`, ubitofu is purpose-built for the
`ubiquiti-community/unifi` provider: it knows which attributes are settable, which are
computed, and which are secrets, so the HCL it emits applies cleanly instead of fighting
the provider schema. Compared with hand-rolled scripts (e.g. `terrifi`-style
one-offs), it is re-runnable and drift-aware — re-run `verify` any time to confirm code
and controller still agree.

## Claude Code workflow skill

The repo ships a Claude Code skill at `.claude/skills/unifi-tofu-reconcile-workflow/`.
Its rule: never hand-author UniFi HCL — draft with `ubitofu` and refine.

If you run Claude Code inside a clone of this repo, the skill is available automatically.

`pip install ubitofu` does not install the skill — PyPI packages do not carry Claude Code
skills. Its best home is your own infrastructure repo: copy the
`unifi-tofu-reconcile-workflow` directory into that repo's `.claude/skills/` and commit it,
so everyone who clones the repo gets it. To use it across every project instead, copy it
into `~/.claude/skills/`.

## License

Licensed under GPL-3.0-or-later — see [LICENSE](LICENSE).

> Also relevant if you searched: unifi terraform import, udm as code, opentofu ubiquiti, ubiquiti-community/unifi provider import.
