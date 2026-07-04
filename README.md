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

It never runs `tofu apply` and never writes to the controller. Every run only reads
from the controller and writes HCL to your working directory, so it is safe to run
against production networks.

## Importing an existing UniFi controller into Terraform/OpenTofu

Four subcommands take you from a live controller to appliable code:

```console
$ ubitofu enumerate --config config.toml   # import blocks + coverage gaps
$ ubitofu generate  --config config.toml   # imports.tf + generated.tf + unifi-variables.tf
$ ubitofu reconcile --config config.toml   # merge drift into committed HCL in place
$ ubitofu verify    --config config.toml   # plan must be clean (or secrets-only)
```

- `enumerate` walks the controller and prints `import` blocks plus a report of anything
  it cannot bring under management.
- `generate` writes `imports.tf`, `generated.tf`, and `unifi-variables.tf` — a
  self-contained, appliable configuration for the `ubiquiti-community/unifi` provider.
- `reconcile` is the comment-preserving counterpart to `generate`: instead of
  regenerating wholesale, it edits your committed, hand-tuned `.tf` in place — updating
  drifted top-level scalars (comments and layout untouched), appending new controller
  objects with their `import` blocks, and flagging complex drift (nested/list/map
  attributes) and controller-side removals for manual review. The printed report is the
  product; nothing is applied.
- `verify` runs a plan and passes only when it is clean (or the only diffs are in
  schema-sensitive attributes whose values live in variables).

Configuration is TOML:

```toml
controller_url = "https://192.168.1.1"
site           = "default"
api_key_source = "op"                                # or "env"
api_key_ref    = "op://YourVault/unifi.api-key/credential"
op_vault       = "YourVault"
workdir        = "./work"
```

## Supported UniFi resources

ubitofu imports the resources the `ubiquiti-community/unifi` provider can manage:
networks and VLANs, WLANs, firewall rules and groups, port profiles, port forwards,
WireGuard VPN servers and peers, clients (by MAC), and devices.

Resources the provider cannot manage — NAT rules, DNS content-filtering, device
adoption, RF/firmware settings — are detected and reported, never silently dropped, so
you always know what remains outside code.

## Secrets

Secret attributes (WLAN passphrases, dynamic-DNS passwords, …) are never emitted as
plaintext. Each one known to the `SECRETS` table renders as a `var.<name>` reference,
and `generate` writes a `unifi-variables.tf` declaring every referenced variable
(`type = string`, `sensitive = true`) so the generated config is self-contained.

Variable **values** are supplied by you from your secret manager — e.g. `TF_VAR_<name>`
environment variables or a git-ignored `*.auto.tfvars`. The tool prints a suggested
secret-manager reference for each variable (rendered with your configured `op_vault`);
references are reporter output only and are never written to files.

Sensitive attributes without a `SECRETS` rule are omitted from the HCL and added to
`lifecycle { ignore_changes }`. As a safety net, any emitted string value that still
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

## License

Licensed under GPL-3.0-or-later — see [LICENSE](LICENSE).

> Also relevant if you searched: unifi terraform import, udm as code, opentofu ubiquiti, ubiquiti-community/unifi provider import.
