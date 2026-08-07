# ubitofu 0.10.1 Provider Contract Port Design

Date: 2026-08-07
Status: proposed for review

## Goal

Port provider-contract admission from the pre-0.10 implementation into the
released 0.10 architecture. Contract mode must prove that one exact provider
binary, OpenTofu-compatible CLI, provider schema, catalog identity, lifecycle
receipt, and differential corpus agree before ubitofu contacts the controller.

The patch retains the 0.10 command surface, semantic planner, runtime session,
receipt model, and transaction path. It does not restore `enumerate`, `verify`,
`migrate`, `reconcile --check`, the old reporter, or the old reconciliation
pipeline.

## Release boundary

Version 0.10.0 is already released from the verified big-bang architecture.
This work targets 0.10.1 on a separate branch. Preparing the patch means
implementing and proving the port, then pushing a candidate branch. Tagging and
publishing 0.10.1 remain separate release actions.

## Approaches considered

### Verified execution object and native differential proof

Add one provider-contract boundary that resolves the configured evidence and
produces a short-lived execution object. The object creates every `TofuRunner`
used by a command and supplies the already-verified schema. Rewrite the runtime
differential check to exercise the 0.10 generation, projection, planning, and
outcome APIs.

This is the selected approach. It preserves one semantic path and makes the
contract an admission check around that path.

### Binary and schema verification only

Port the sidecar, binary, CLI, and schema hashes but omit the differential
corpus. This is smaller, but it drops the part of the contract that proves
ubitofu interprets the admitted provider behavior correctly. It would be an
incomplete port and would give stronger assurance in the configuration than in
the behavior that consumes it.

### Compatibility adapter around the old pipeline

Retain the old contract modules and make the 0.10 CLI call them before the new
pipeline. The old differential implementation depends on retired generation,
existence, reporting, and migration helpers. Keeping those helpers would create
a second semantic implementation and violate the 0.10 cutover boundary.

## Public configuration

Contract mode is opt-in through exactly four configuration keys:

```toml
provider_contract = "./provider-contracts/unifi_dns_record.v1.json"
provider_contract_checksum = "./provider-contracts/unifi_dns_record.v1.sha256"
provider_binary = "./tools/terraform-provider-unifi"
provider_schema_cli = "tofu"
```

All four keys must be present or all four absent. Relative paths resolve from
the configuration file's directory, not the process working directory. The
selected CLI may be `tofu`, `opentofu`, or `terraform`. The implementation
resolves and hashes the actual executable. Callers cannot supply claimed CLI
versions, hashes, or schema files.

The removed `provider_schema`, `provider_schema_cli_version`, and
`provider_schema_cli_sha256` compatibility keys do not return. Unknown keys
continue to fail during typed configuration loading.

Without the four keys, commands use the ordinary `tofu` runner and current
provider-schema behavior. Contract mode adds no new command and changes no
successful human or JSON output.

## Architecture

`provider_contract.py` owns evidence admission only. It validates the sidecar,
contract document, provider binary, CLI identity, canonical provider schema,
catalog identity, manifest resource declaration, redaction requirements, and
passing lifecycle receipt. It does not enumerate controller objects, generate
HCL, classify reconciliation, render reports, or write the worktree.

The module exposes a context-managed `ProviderExecution` with two operations:

```python
@dataclass(frozen=True)
class ProviderExecution:
    contract: ResolvedContract | None
    schema: dict[str, object] | None

    def runner(
        self,
        *,
        workdir: Path,
        plan_path: Path | None = None,
    ) -> TofuRunner: ...


@contextmanager
def provider_execution(
    *, cfg: Config, workdir: Path
) -> Iterator[ProviderExecution]: ...
```

The concrete object privately retains the resolved CLI path, scoped provider
override environment, immutable schema source, and temporary-directory owner.
Leaving the context destroys the selected provider copy and CLI configuration.
No caller receives those implementation details.

`TofuRunner` gains optional environment and verified-schema inputs. `_exec`
passes the environment explicitly. `providers_schema()` returns a detached
copy of the verified schema when present and otherwise invokes the CLI as it
does in 0.10.0. Every runner created by a contract execution therefore uses
the same selected CLI, provider override, and schema evidence.

The four provider-backed pipeline commands enter contract execution after
acquiring any existing runtime lock and before creating a controller client:

```text
validated config
      |
runtime session where the command already owns one
      |
provider contract admission
      |
one ProviderExecution
      |
controller collection + existing 0.10 pipeline
```

`generate`, `reconcile`, `check`, and `inspect` receive runners only through
that execution object. `health snapshot` and `health compare` do not consume a
provider schema and do not run provider-contract admission.

Contract admission may read the OpenTofu module and execute read-only CLI
commands. It may create only its private mode-0700 system temporary directory.
It does not publish a root-module file, mutate state, or invoke an OpenTofu
mutation command. The existing `TofuRunner` command guard remains authoritative.

## Differential contract

`contract_diff.py` retains the versioned DNS corpus format and packaged corpus,
but its evaluator is rewritten around 0.10 APIs. It must not copy the old
attribute builder, existence classifier, secret scanner, or receipt digest
logic.

Each corpus case is converted into the smallest immutable 0.10 inputs needed
to exercise the real code paths. The evaluator calls the same provider-schema
normalization, controller projection, generation rendering, reconciliation
planner, secret suppression, and outcome digest helpers used by commands. It
then compares the case's declared support, plan outcome, import identities,
redacted paths, generated HCL digest, and receipt-input digest.

If a corpus assertion cannot be expressed through a stable 0.10 public-internal
interface, the implementation adds a narrow pure function at the owning module
boundary. It does not reach into command orchestration or add a contract-only
semantic implementation.

The packaged corpus is immutable for contract format version 1. Updating an
expected digest requires a new reviewed contract/corpus version rather than a
test update that silently accepts changed behavior.

## Failure behavior

Missing files, unsafe paths, malformed JSON, incomplete configuration, digest
mismatches, unsupported contract versions, provider or CLI identity mismatches,
schema mismatches, manifest disagreement, failed lifecycle evidence, and
differential mismatches raise one typed `ProviderContractError`.

The CLI renders that error through the existing bounded safe-error path and
returns exit 2. Messages name the failed evidence category but never include
contract contents, controller data, environment values, command output, or
unbounded filesystem paths. Contract admission finishes before controller
construction, so every contract failure makes zero controller requests and
zero worktree writes.

Cleanup runs on success, mismatch, command failure, and cancellation. A cleanup
failure cannot replace the primary safe error with a traceback or leak a
temporary provider path into normal output.

## Receipts and compatibility

The patch does not change receipt schema or successful output. Existing
provider-schema digests continue to describe the schema consumed by generation
and inspection. Contract mode guarantees that the consumed schema came from
the admitted CLI and provider binary.

Version 0.10.1 remains a patch because the default configuration and command
surface are unchanged. Contract mode is a new opt-in validation boundary. It
does not offer compatibility with the pre-0.10 commands or configuration
claims.

## Tests and release proof

Unit tests cover all-or-none configuration, config-relative path resolution,
CLI and provider executable resolution, sidecar parsing, canonical schema
hashing, toolchain identity, manifest and lifecycle checks, cleanup, and safe
errors. Every failure test asserts that controller construction was not called.

Runner tests prove that all command invocations receive the selected binary and
environment, cached schemas are detached between consumers, and forbidden
OpenTofu commands remain forbidden.

Differential tests execute every packaged DNS case through the 0.10 semantic
functions. A guard test fails if the corpus evaluator imports a retired module
or defines duplicate planner, renderer, secret, or receipt algorithms.

Pipeline tests cover contract and non-contract execution for `generate`,
`reconcile --dry-run`, wet `reconcile`, `check --plan`, and `inspect`. Health
tests prove that provider contract configuration does not invoke provider
admission for health-only commands.

The candidate gate is the ordinary static checks, complete baseline suite,
package installation proof, gitleaks, targeted mutation gate for every changed
correctness-critical module, and one manual serialized Woodpecker run including
controller tests and the full mutation sweep. No 0.10.1 tag is created until
those gates pass.

## Non-goals

- restoring any pre-0.10 command or exit code
- accepting caller-supplied schema, CLI-version, or hash claims
- supporting more provider addresses or resource contracts in this patch
- changing the contract document or DNS corpus format
- applying OpenTofu plans or mutating state
- adding Ansible, CI-vendor, or secret-manager policy to ubitofu
