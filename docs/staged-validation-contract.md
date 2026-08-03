# Offline staged-validation contract

Status: proved on macOS 15.7.5 arm64 and Linux arm64 with OpenTofu 1.12.0.
The bounded cross-platform architecture gate is closed for these two enforced
execution routes.

## Preconditions

The source module must already be initialized. Its root must contain the lock
file and the installed provider and registry-module data under `.terraform`.
Missing data fails with `initialize first`. Staged validation never runs
`tofu init` or downloads a replacement.

Local module sources must use relative `./` or `../` paths and must resolve to
ordinary directories. The harness rejects a symlink in any traversed path
component. Absolute local modules are also rejected.

The bounded proof rejects `file`, `filebase64`, `fileexists`, `fileset`,
`templatefile`, and other `file*` function calls found in parsed configuration.
It does not guess which auxiliary files an expression might read.

## Effective manifest

The proof stage contains only:

- active root `.tf`, `.tofu`, `.tf.json`, and `.tofu.json` files
- `.tofu` in preference to a same-stem `.tf`, separately for native and JSON
  syntax, including `override` and `_override` names
- module sources after OpenTofu-style override merging by module label
- `.terraform.lock.hcl`, filtered to providers required by the effective
  staged configuration
- recursively resolved relative local modules, using the same relative layout
  as the source root
- filtered `.terraform/providers` packages for exact required source/version
  pairs
- a filtered `.terraform/modules/modules.json` and only the registry module
  active configuration files selected by effective module keys

Root configuration, the provider lockfile, and module-cache metadata must be
regular files. Each is checked with `lstat`, opened with `O_NOFOLLOW`, and
verified with `fstat` before reading. Active candidate bytes come from memory
and do not read a same-path destination file.

Each registry module `Dir` in `modules.json` must be normalized and relative.
The resolved source must stay below the destination `.terraform/modules`, and
the independently resolved target must stay below the staged
`.terraform/modules`. Absolute and escaping paths fail before any stage write.

Selected provider version trees reject a symlink in any source component or
package entry. The non-following copy preserves a raced symlink rather than
following it, and a second staged-tree check rejects it before OpenTofu runs.
Required provider sources must contain exactly hostname, namespace, and type
components. Empty, dot, dot-dot, embedded separator, and malformed components
are rejected. The locked version must also be one safe path component. Source
and target paths are normalized from independent provider roots and must stay
below the destination and staged `.terraform/providers` trees respectively.

Root candidates are byte payloads keyed by a single root configuration file.
A new `.tofu` candidate can shadow an existing same-stem `.tf` file. The proof
harness does not stage state, plans, variable-value files, credentials, `.git`,
or unrelated root files.

## Offline validation

`validate_staged_module` creates a mode-0700 temporary tree and a private
`TF_DATA_DIR`. It copies inputs rather than hardlinking them, then runs only:

```text
tofu validate -no-color
```

The subprocess receives `TF_IN_AUTOMATION=1`, `CHECKPOINT_DISABLE=1`, and a
private CLI configuration whose only provider installation method is an empty
filesystem mirror. Uppercase and lowercase HTTP, HTTPS, and all-proxy variables
point to `127.0.0.1:1`. Uppercase and lowercase bypass variables are empty.

On macOS, both OpenTofu and its provider subprocesses run under
`sandbox-exec`. The profile denies outbound IP sockets while allowing the Unix
domain socket used for provider plugin IPC. Before validation, the harness runs
a real loopback connection attempt under the same profile. The probe accepts
only `EPERM`, emits `UBITOFU_NETWORK_DENIED:EPERM`, and exits 73. The verifier
requires that exact status and sentinel. A `sandbox_apply` or profile error
fails explicitly and cannot satisfy the probe.

On Linux, the same subprocesses run under `/usr/bin/unshare --user
--map-root-user --net --`. The new user namespace scopes the child's
administrative capabilities away from the parent namespace. The new network
namespace then applies to OpenTofu and every provider child. It has no usable IP
interface or route, but Unix domain sockets remain available for provider plugin
IPC.

The preflight probe first tries to open or join `/proc/1/ns/net`. It requires
`EACCES` or `EPERM` and emits `UBITOFU_PARENT_NETNS_DENIED` only after the escape
attempt fails. It then makes a real loopback connection attempt, requires
`ENETUNREACH`, emits `UBITOFU_NETWORK_DENIED:ENETUNREACH`, and exits 73. A
successful parent-namespace join, missing `unshare`, denied namespace creation,
unexpected errno, or mismatched status or sentinel stops before OpenTofu runs.

The Linux launcher must have permission to create the user and network
namespaces. The proof container received `CAP_SYS_ADMIN`, but OpenTofu and its
providers run after entering the new user namespace and cannot exercise that
capability against the parent-owned network namespace. The same container
without the launch capability failed with `unshare: unshare failed: Operation
not permitted`. The harness reports either failure and does not fall back to
unsandboxed validation.

The current Codex workspace sandbox does not permit a nested `sandbox-exec`
profile. The macOS proof tests therefore ran in the approved host-level
execution context. The Linux proof ran in a disposable arm64 container with
permission to create the required namespace. Running either harness route in a
restrictive parent context produces an explicit network-enforcement error
rather than unsandboxed validation.

The checked fixture also declares an HTTP backend and an HTTP provider data
source at closed `127.0.0.1:1` endpoints. Bootstrap uses
`tofu init -backend=false`. Staged `tofu validate` succeeds without touching
either trap. The test hashes every regular file and records `lstat` identity,
mode, size, and modification time for the destination tree before and after
validation.

## Proven fixture surface

The macOS integration fixture covers all four configuration suffixes,
same-stem native and JSON precedence, `override` and `_override` precedence, a
lock file, nested modules inside and outside the root, the cached
`cloudposse/label/null` 0.25.0 registry module, and cached `hashicorp/http`
3.5.0 and `hashicorp/random` 3.7.2 providers. The candidate replaces a file
that is invalid on disk, so a successful validation proves OpenTofu saw the
candidate bytes.

The cache test injects an unrelated registry module, provider package, and
lockfile entry after initialization. None appears in the staged cache. A
separate override fixture replaces `../unused` with `./safe` for the same
module label and proves that only `./safe` is staged.

Selected registry modules are parsed and checked before any package content is
copied. The stage receives only active `.tf`, `.tofu`, `.tf.json`, and
`.tofu.json` configuration. It excludes `.git`, state, variable-value files,
and unrelated package payloads. A regression test places an unreadable payload
beside a cached registry configuration containing `file("/etc/hosts")`. The
filesystem-function diagnostic wins before the payload can be read or copied.

Active module configuration must be a regular file in the selected package.
The reader uses `lstat`, rejects a symlink, opens with `O_NOFOLLOW`, and checks
the opened file's type, device, and inode with `fstat` before reading. Native
and JSON regressions point active cached configuration at unreadable files
outside the package and receive the symlink diagnostic without reading them.

The fixture bootstrap may contact the OpenTofu registry and GitHub in its
disposable destination. That bootstrap is not part of staged validation.

## Proof execution

The explicit regression suite lives at `proofs/test_staged_validation_proof.py`,
outside the default pytest collection path. Normal CI does not grant Linux
namespace capabilities, so it must not collect this suite.

Run the suite on macOS from a host context where `sandbox-exec` is not blocked by
an outer sandbox. On Linux, run it in an isolated proof environment with OpenTofu
1.12.0, Git for the fixture bootstrap, and permission to create a network
namespace. Passing proxy variables or Docker `--network=none` does not satisfy
the proof. The harness must create and verify its own enforced boundary.

The Linux recipe is retained as `proofs/staged-validation-linux.Dockerfile`.
It pins the Python base image by digest, proof dependencies by version, OpenTofu
1.12.0, and the `linux_arm64` archive SHA-256. Reproduce the recorded run from
the repository root:

```text
docker build --file proofs/staged-validation-linux.Dockerfile \
  --tag ubitofu-staged-proof:2026-08-03 .
docker run --detach --name ubitofu-staged-proof \
  --cap-add SYS_ADMIN ubitofu-staged-proof:2026-08-03
docker exec --workdir /proof ubitofu-staged-proof \
  python -m pytest proofs/test_staged_validation_proof.py -q -ra
docker rm --force ubitofu-staged-proof
```

The test bootstrap uses the container's ordinary bridge network. The harness's
inner user and network namespaces enforce the validation boundary.

## Linux proof record

The Linux run used Python base image digest
`sha256:57cd7c3a7a273101a6485ba99423ee568157882804b1124b4dd04266317710de`,
a Debian trixie arm64 userspace, Python 3.12.13, and the `6.8.0-117-generic`
aarch64 kernel. OpenTofu 1.12.0 `linux_arm64` matched SHA-256
`466bf912404b4ab0f0b3a043073d68ad34f11d55ad7a483957d94f0733169f8d`.
Fixture bootstrap ran with ordinary container networking. Each staged `tofu
validate`, `tofu version`, provider plugin, parent-namespace escape probe, and
socket-denial probe then ran below the harness's own user-and-network namespace
wrapper.

All 25 staged-validation tests passed on Linux. The success fixture exercised
the cached `hashicorp/http` and `hashicorp/random` providers and the cached
`cloudposse/label/null` registry module. Provider Unix-socket IPC completed in
the network namespace. The HTTP backend and provider endpoints remained
unreachable, no dependency download path was available during validation, and
the destination snapshot was byte-for-byte and metadata-identical after the
run.

## Gate verdict

The repaired bounded contract passes on both recorded platforms with
kernel-enforced outbound-network denial. The macOS-and-Linux staged-validation
gate is closed. Linux execution still requires permission to create the network
namespace. Hosts that deny it fail closed before validation.
