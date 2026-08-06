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

Five subcommands take you from a live controller to appliable code, and keep it
appliable across provider upgrades:

```console
$ ubitofu enumerate --config config.toml   # import blocks + coverage gaps (requires tofu-init'd workdir)
$ ubitofu generate  --config config.toml   # imports.tf + generated.tf + unifi-variables.tf
$ ubitofu reconcile --config config.toml   # merge drift into committed HCL in place
$ ubitofu reconcile --check --config config.toml   # gate: classify only, write nothing, same exit codes
$ ubitofu verify    --config config.toml   # plan must be clean (or secrets-only)
$ ubitofu migrate   --config config.toml   # what a provider bump breaks, before it plans
```

- `enumerate` walks the controller and prints `import` blocks plus a report of anything
  it cannot bring under management. Requires a tofu-init'd `workdir` — it reads the
  provider schema for the coverage audit.
- `generate` writes `imports.tf`, `generated.tf`, and `unifi-variables.tf` — a
  self-contained, appliable configuration for the `ubiquiti-community/unifi` provider.
- `reconcile` edits your committed, hand-tuned `.tf` in place, preserving comments and
  layout. Committed resource blocks decide what should exist. Reconcile imports matching
  live objects into configured addresses, appends genuinely new controller objects, and
  reports pending creates, destroys, and forgets without blocking apply. Ambiguous
  identities and replacement plans require review. State-only objects are never inferred
  back into config. Re-runs do not duplicate resources or imports. Nothing is applied.
- `verify` runs a plan and passes only when it is clean (or the only diffs are in
  schema-sensitive attributes whose values live in variables).
- `migrate` compares the installed provider's schema against a baseline in
  `<workdir>/.ubitofu/provider-baseline.json` and reports what a version bump
  breaks — see below. It leaves the controller and your `.tf` untouched.

### Provider upgrades

When a provider drops an attribute your config still sets, `tofu plan` fails
with "Unsupported argument". That is too early for `reconcile`, which needs a
plan to read. Run `migrate --write-baseline` once on your current version, then
again after the bump:

```console
$ ubitofu migrate --config config.toml --write-baseline   # today's provider
$ # ... bump the version, tofu init -upgrade ...
$ ubitofu migrate --config config.toml
Provider migration: registry.terraform.io/example/unifi 0.57.0 -> 0.101.1
Blocking — the plan fails until these are resolved:
  - removed-attr unifi_device.radio_table.assisted_roaming_enabled: removed —
    a config that sets it fails to plan; set in unifi-devices.tf:363, …
Review — plan against live before applying:
  - new-attr unifi_wlan.roaming_assistant_na_enabled: new — plan against live
    before applying: the schema JSON cannot show whether it carries a default
    that would override the controller's value
```

`migrate` reports only what your committed HCL can hit, and names the
`file:line` of every assignment a removal forces you to change. It edits
nothing. Removed attributes are often nested, and the surgeon edits only
top-level scalars.

Commit the baseline alongside your HCL. It names the provider schema your
config last matched, and CI needs it to diff the next bump.

The review section exists because the schema JSON carries no defaults. A new
attribute that will override a live controller value looks exactly like one
that will not. `migrate` therefore names it and stops. `reconcile` finishes the
job: it plans against the live controller, and writes in any live value a
provider default would otherwise overwrite.

### Exit codes

Every subcommand uses the same flat, rsync-style scheme — distinct small codes
you can `case` on, no report-grepping:

| code | meaning |
|-----:|---|
| 0    | success — in sync / clean plan / nothing to report |
| 10   | drift captured — committed `*.tf` edited, an import emitted, or a new object appended (`reconcile`) |
| 11   | attention required — reconcile finding (complex drift / existence / replacement / invariant / secret), verify drift, or migrate schema finding |
| 12   | drift captured AND attention required |
| 13   | forbidden device create — remove the block or adopt via UI (`reconcile`) |
| 20   | cannot reach or use the controller — transport failure, or an error response that is not an auth rejection. Retrying is reasonable |
| 21   | authentication failed — the controller rejected the credentials (401/403). Retrying will not help |
| 22   | secret unavailable — `op read` failed. Run `op signin`, or check `api_key_ref` |
| 23   | tofu failed — init, plan, schema, or fmt. After a provider bump, `ubitofu migrate` names what broke |
| 1    | unexpected error — please report |
| 2    | usage error |

`1x` is an outcome the run reached; `2x` is a reason it never got there. A
wrapper that only cares whether the run worked still tests for nonzero.

Under `set -e`/`pipefail`, capture the code instead of aborting:

```bash
rc=0; ubitofu reconcile --config config.toml | tee report.txt || rc=$?
case "$rc" in
  0)  ;;                                          # nothing to do
  10) open_pr ;;                                  # drift captured
  11) notify "manual attention needed" ;;
  12) open_pr; notify "manual attention needed" ;;
  13) die "device create planned — remove the block or adopt in the UI" ;;
  20) warn "controller unreachable — will retry next run" ;;
  21) page "UniFi credentials rejected" ;;
  22) die "no secret — op signin, or check api_key_ref" ;;
  23) ubitofu migrate --config config.toml ;;   # provider bump? name what broke
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

Provider contract mode is an explicit development opt-in. These four values
select the exact sidecar, provider binary, and schema CLI. ubitofu resolves and
hashes the executable, reads its version, installs a scoped development
override for the selected provider binary, and queries the provider schema from
the configured workdir. A mismatch stops before any controller request:

```toml
provider_contract          = "./provider-contracts/unifi_dns_record.v1.json"
provider_contract_checksum = "./provider-contracts/unifi_dns_record.v1.sha256"
provider_binary            = "./tools/terraform-provider-unifi_v0.101.2"
provider_schema_cli        = "./tools/terraform"
```

Without these keys, ubitofu keeps using its legacy manifest. Contract mode
currently runs beside that manifest and requires exact DNS-record mapping
parity; it does not read the provider's structural catalog or code-generation
inputs.

The full-catalog evidence gate is separate from that runtime opt-in. Release CI
can verify a provider-produced management contract without contacting a
controller or changing HCL, state, imports, or routing:

```sh
python -m ubitofu.catalog_contract \
  --contract ./evidence/catalog-management-contract.json \
  --contract-sha256 "$PINNED_CONTRACT_SHA256" \
  --provider-binary ./evidence/terraform-provider-unifi \
  --terraform ./evidence/terraform \
  --terraform-schema ./evidence/terraform-schema.json \
  --tofu ./evidence/tofu \
  --tofu-schema ./evidence/tofu-schema.json \
  --repository-root . \
  --output ./evidence/ubitofu-catalog-contract-parity.json
```

The verifier hashes every local input, checks the pinned manifest at the exact
Git commit named by the contract, and requires the Terraform and OpenTofu
schema projections to match the provider catalog. Its mode-0600 receipt covers
all 67 surfaces: the 28 managed resources bind to `MANIFEST`; data sources,
list resources, and actions are recorded as not applicable to controller
capture. The `unifi_port` hardware claim remains visible as a release blocker
until physical evidence closes it.

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
