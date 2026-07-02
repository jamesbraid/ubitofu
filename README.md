# unifi-tofu-import

Re-runnable CLI that enumerates a live UniFi/UDM controller and emits clean,
directly-appliable **OpenTofu** HCL for the `ubiquiti-community/unifi` provider.

Bulk-import an existing controller into OpenTofu, then re-run to reconcile code
with live config and surface drift. Plan-only and non-mutating — never runs
`tofu apply`, never writes to the controller.

## Usage

```console
$ unifi-tofu-import enumerate --config config.toml   # import blocks + coverage gaps
$ unifi-tofu-import generate  --config config.toml   # imports.tf + generated.tf + unifi-variables.tf
$ unifi-tofu-import verify    --config config.toml   # plan must be clean (or secrets-only)
```

Config is TOML:

```toml
controller_url = "https://192.168.1.1"
site           = "default"
api_key_source = "op"                                # or "env"
api_key_ref    = "op://YourVault/unifi.api-key/credential"
op_vault       = "YourVault"
workdir        = "./work"
```

## Secrets and variables

Secret attributes (WLAN passphrases, dynamic-DNS passwords, …) are never
emitted as plaintext. Each one known to the `SECRETS` table renders as a
`var.<name>` reference, and `generate` writes a `unifi-variables.tf`
declaring every referenced variable (`type = string`, `sensitive = true`)
so the generated config is self-contained.

Variable **values** are supplied by you from your secret manager — e.g.
`TF_VAR_<name>` environment variables or a git-ignored `*.auto.tfvars`.
The tool prints a suggested secret-manager reference for each variable
(rendered with your configured `op_vault`); references are reporter output
only and are never written to files.

Sensitive attributes without a `SECRETS` rule are omitted from the HCL and
added to `lifecycle { ignore_changes }`. As a safety net, any emitted string
value that still looks secret-shaped (secret-bearing attribute name, or a
44-char base64 WireGuard-key shape) is suppressed the same way, with a loud
warning naming the resource and attribute — add a `SECRETS` rule to manage
it properly.

`verify` passes on a clean plan, and also on a plan whose only diffs are in
schema-sensitive attributes (their values live in vars that cannot be
compared against controller state).

## Non-goals

Resources the provider can't manage (NAT rules, DNS content-filtering, device
adoption, RF/firmware) are detected and reported, not managed.

## License

Licensed under GPL-3.0-or-later — see [LICENSE](LICENSE).
