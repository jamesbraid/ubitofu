# ubitofu Roadmap: Simplify, Refactor, Harden

Date: 2026-09-05
Status: proposed

This roadmap follows the 0.10.0 release of 2026-08-07. It records what the
upstream projects shipped in the four weeks since, what ubitofu must change to
consume them, and where the 0.10 implementation should shrink or harden next.
Every claim below was measured on 2026-09-05 unless it says otherwise.

## Where 0.10 stands

The 0.10 architecture is in and released: immutable snapshots, a semantic
planner, byte-anchored HCL edits through a tree-sitter index, a recoverable
file transaction, versioned JSON receipts, and one exit scheme. Since late
July that is 153 commits and about 46,000 added lines. Only ten commits landed
after 2026-08-05, all release and CI ordering work.

Five facts shape the order of everything below.

1. **0.10 has no consumer yet.** The Ansible repository still pins ubitofu
   v0.9.1. Its apply gate calls `reconcile --check` and branches on exit codes
   10 through 13, both removed in 0.10. The single Ansible cutover promised in
   the 0.10.0 changelog has not started.
2. **`generate` blocks on every real site.** The 0.10 rewrite of generation
   (commit 615efe0, 2026-08-04) treats enumeration skips as blocking coverage
   gaps. A fresh site on the pinned seeded image always carries a default
   RADIUS profile, a default client-QoS user group, and the built-in "All APs"
   group, and has no BGP configuration. The enumerator deliberately skips
   the first three and records the absent singleton, then reports all four
   as gaps, so generation ends in `generation_blocked` on any controller.
   0.9.1 listed the same skips in COVERAGE.md and carried on. No unit test
   pins the 0.10 behaviour, the JSON receipt shows the gaps only as opaque
   references with no subject, and `inspect` does not list enumeration gaps
   at all. An operator cannot learn from ubitofu's own output why generation
   stopped. Controller 10.6.x adds a fifth blocker: it creates a nightly
   `quick-scan` scheduled task in every new site, which is a `rest/scheduletask`
   object with no provider resource until `unifi_schedule_task` is released.
3. **`reconcile` cannot succeed on an adopted site either.** Measured on
   2026-09-06 with a provider build that imports cleanly (below). Reconcile
   first crashes with "unexpected internal error" because every resource
   carries `timeouts = null` in state after import and the projection in
   `controller_projection.py` rejects a null nested single object as
   invalid. The schema has declared that attribute on all 28 resources since
   at least 0.101.1, and the message's hint to "rerun with local debug
   logging" points at nothing: the CLI has no debug switch. With null nested
   objects treated as absent, reconcile completes and blocks with
   `incomparable_controller_observation` on both networks and both sites.
   The rule requires every leaf path of the managed state to be comparable
   against the live record, and comparable paths come from projecting the
   raw controller JSON through the provider schema, so only fields whose
   controller name equals the provider attribute name survive. The manifest
   declares one controller-to-provider field policy in total. For a network,
   `enabled`, `domain_name`, `gateway_type`, and the whole `dhcp_server`
   block are all required and none is comparable. The unit suite passes
   because its fixtures use matching names.
4. **The proof chain on main is red.** Of the last four cron runs of the
   serialized `ci -> controller -> mutation` chain, three failed. Two ended
   with steps in state `killed`, the signature of the Woodpecker SQLite lease
   cancellation already recorded against this server. One failed in mypy
   because python-hcl2 8.1.3 ships type stubs and `hcl_writer.py` calls its
   `Builder` with keyword expansion the stubs reject. Development dependencies
   are unpinned, so a cron run proves whatever PyPI served that morning.
5. **Two branches hold unmerged contract work.** `emdash/provider-contract-0.10.1`
   (11 commits, +3,579 lines, a 674-line DNS corpus shipped inside the wheel)
   and `emdash/catalog-contract-parity` (3 commits, +949 lines). Both predate
   provider 0.105 and the measured-behaviour artifact the provider now
   publishes, and the provider maintainers record no contract with ubitofu
   beyond the registry artifact and `tofu providers schema -json`.

## Upstream refresh

Every upstream follows a single-tag-per-cycle rule: pin released tags only.
Provider main after v0.109.0 and unifi-containers main after 2026-09-03 are
unreleased and must not be pinned. The next tags land together at cycle end.

| Pin | ubitofu today | Latest release | Move to |
| --- | --- | --- | --- |
| `unifi-network` image | 10.4.57 (build -4) | 10.6.101 | 10.6.101 by digest |
| `unifi-os-server` image | 5.1.21 (build -6) | 5.1.40 (revision -1) | 5.1.40 by digest |
| unifi-emu herder and synthetic image | 0.5.1 | 0.5.5 | 0.5.5 |
| provider `jamesbraid/unifi` | 0.101.1 | 0.109.0 | 0.109.0 |
| go-unifi | not consumed directly | v1.113.0 | nothing to pin |

Digests served by GHCR on 2026-09-05:

```text
unifi-network:10.6.101-sim      sha256:b4adfe9427c2a4fc90cb236212a481f78440fa83625a0e06f9c9081730c6c78e
unifi-network:10.6.101-seeded   sha256:44261037f254f54d5a4ccc88b693b4ef3b939e8948c1640e3882036cd60da5f0
unifi-os-server:5.1.40-sim      sha256:0907e1f2953203cb41435b015fd0f2e3e109b345f41c9900914a502f67578de7
unifi-os-server:5.1.40-seeded   sha256:1235e42f61b5b73d20faaa559e19209a8a7ce38f42efa4135577561da303890d
```

Re-query the registry immediately before merging a pin change. Image tags
stay fixed while packaging revisions rebuild them, so a digest moves without
the tag changing.

What changes in behaviour when the pins move:

- **Controller 10.6.101 and UOS 5.1.40.** `UNIFI_TEST_EXPECT_VERSION` becomes
  `10.6.101`. UOS 5.1.40 bundles Network 10.5.67, measured by booting the
  published image, so `UNIFI_TEST_UOS_EXPECT_VERSION` becomes `10.5.67`. The
  next unifi-containers release updates the bundled app to the current
  release at build time and exposes it as the OCI label
  `org.unifi-containers.network-version`, so the expected version should then
  be read from the label instead of pinned by hand.
- **Seeded UOS API key.** The `-seeded` UOS image mints an admin X-API-KEY at
  boot and writes it to `/unifi/api-key`, and its healthcheck gates on that
  key authenticating. ubitofu's `uos-seeded` flavor already reads that file.
  The `UNIFI_TEST_UOS_SEEDED_KEY` override and the 150-line probe transcript
  in `tests/controllertest/uos.py` describe the 5.1.21 gap and can go.
- **Herder 0.5.5.** 0.5.3 skips the image healthcheck check under podman,
  0.5.4 publishes the synthetic image in Docker manifest format, and 0.5.5
  changes nothing at runtime. The protocol ubitofu drives is unchanged. Move
  `EMU_VERSION` and the GitHub Actions `install-herder@` reference together.
- **Provider 0.109.0.** `unifi_network` moved to masked field updates in
  0.109.0, which the provider maintainers expected to fix both import bugs in
  `docs/provider-import-bugs.md`. The retest in the next section shows one
  narrowed and one moved. 0.109.0 also models twelve more `unifi_setting`
  sections.
  Measured against a fresh site on the 10.6.101 seeded image, the coverage
  audit reports 36 setting gaps on 0.101.1 and 34 on 0.109.0: whole sections
  closed, but field-level gaps opened inside `ether_lighting`,
  `global_switch`, `guest_access`, `mdns`, and `radio_ai`, and `connectivity`,
  `device_supervision`, `element_adopt`, `peer_to_peer`, `ugw`, and `usg_geo`
  remain unmodelled. Provider error text no longer carries the request body.
  ubitofu never parsed that body, so only log content changes.
- **go-unifi v1.113.0.** Nothing for ubitofu to consume. ubitofu's only
  upstream is the provider: the registry artifact, `tofu providers schema
  -json`, plan and state JSON, and the management contract the provider's
  roadmap commits to publishing beside each resource. go-unifi's new exports
  (the controller's own sensitive-field declaration and a measured-behaviour
  artifact) are the provider's inputs. They reach ubitofu as `sensitive`
  flags in the served schema and as plan behaviour, and must not be read
  directly: what matters to ubitofu is what the provider does with a
  controller fact, and the two can differ.

### Retest of the parked import bugs

The parked in-sync scenario was run on 2026-09-05 against provider 0.109.0
with the 10.6.101 seeded image, and as a control against the current pins.
Both stopped at ubitofu's own coverage gate before reaching `tofu apply`, for
the reasons in fact 2 above. With the gate silenced by a test-only pytest
plugin, the control reproduced both documented bugs exactly, and the new pins
gave this:

- **Bug 1 narrowed, and its mechanism is different from what the bug
  document says.** Import Read on 0.109.0 round-trips `setting_preference`
  and `ipv6_interface_type`. The plan after import is still `2 to change`,
  adding `dhcp_server.leasetime = "24h0m0s"` and `gateway_type = "default"`
  on both networks. Those are not live values: the controller returns
  neither key for either network. They are static schema defaults on
  Optional+Computed attributes in the provider's generated network schema,
  so import stores null and every later plan applies the default. The
  ordinary network is `setting_preference = "manual"` and shows the same
  diff, which rules out the provider's ownership rule. The forced update on
  the default network is still rejected with
  `api.err.DisablingDefaultNetworkNotAllowed`. The controller stores no
  `enabled` key for that network and the provider reads it as `false`, then
  writes it back.
- **Bug 2 moved.** `domain_name` no longer flips from null to `""`. The same
  check now fails on the ordinary network for `dhcp_server.start` (null in
  the plan, `"10.99.210.6"` after apply) and `dhcp_server.stop` (null, then
  `"10.99.210.254"`). The controller does not invent those values: go-unifi's
  network encoder derives a DHCP range from the subnet on every update, so the
  provider's own write plants them and its decode then reports them.

Both were reported to the provider maintainers with this reproduction. Their
fix, provider commit 7946fc06, was built locally and run through the same
scenario on 2026-09-06 with a `dev_overrides` harness: the post-import plan
is `0 to change` and the apply is clean. It merged to provider main the same
day as 94c36a88 (provider PR #40) and is unreleased until the next tag. The two defaults became `UseStateForUnknown` and the DHCP decode
keeps prior nulls. Decoding an absent `enabled` as true is an SDK change and
is still open, so a default network that needs a real change may still send
`enabled:false`. This scenario cannot exercise that case. Once that commit is
tagged, the provider side of the parked scenario is closed. The scenario then
fails inside ubitofu, per fact 3, so the write scenarios now wait on Phase 0
and Phase 2 below rather than on the provider.

## Phase 0: make main green and consumable

Nothing else is worth doing while the released code has no production user
and the proof chain cannot tell a regression from a bad morning.

Progress on 2026-09-11, unreleased on the roadmap branch: generation no longer
blocks on enumeration skips, a null nested object projects as absent, managed
resources are compared through the provider's plan-time read, ubitofu emits
its own HCL with python-hcl2 demoted to a development extra, and every CI
install pins the toolchain through `ci/constraints.txt`. The parked in-sync
scenario passes end to end against a local build of provider main with only
the setting-coverage gaps silenced. Still open: the Woodpecker SQLite lease,
receipt subjects for coverage gaps, a debug switch, stderr assertions in the
scenarios, and the Ansible cutover.

- Pin the development and proof toolchain. A lock file or constraints file
  for `[dev]` and `[controller]`, updated deliberately, so cron proves the
  code and not PyPI.
- Fix `hcl_writer.py` against typed python-hcl2, or remove python-hcl2. It is
  used only for `dumps` on a builder, followed by string splicing of the
  `lifecycle` block that python-hcl2 cannot express. The generation output is
  already validated structurally by the transaction layer, so a small
  purpose-built emitter removes a dependency and the review finding that
  asked for its removal criterion.
- Ask for the Woodpecker server to move off SQLite, or accept that `killed`
  chain runs must be restarted by hand. Until then, a red cron run needs a
  human to read step states before it means anything.
- Fix generation blocking on enumeration skips. Deliberate skips are
  documented, unmanageable defaults, not lost coverage. Report them as
  accepted exclusions with a subject, keep endpoint and field gaps blocking,
  and add the unit test that was missing.
- Give every coverage gap in the generate receipt a subject, and make
  `inspect` list enumeration gaps. An operator who is blocked must be able to
  read why from ubitofu alone.
- Treat a null nested object in state as absent during projection, with a
  unit test that carries `timeouts = null` on every resource. Give the
  "unexpected internal error" path a real switch, an environment variable
  that prints the traceback to stderr, or drop the hint.
- Make the controller scenarios assert on stderr as well as stdout. The
  parked scenario asserted on an empty stdout and hid the crash message.
- Decide the comparability contract before touching the planner. Either the
  manifest carries a controller-to-provider field mapping per resource, which
  is what the current rule assumes and which does not exist, or comparable
  paths are derived from the provider's own read (the plan-time live value
  ubitofu already collects) and the raw controller record is used only for
  existence and identity. The second needs no per-field tables and is the
  direction Phase 7 points at. Until one is chosen, `reconcile` blocks on
  every site with a network.
- Run the Ansible cutover as one change: pin 0.10.x, consume JSON receipts
  and the 0/1/2/3 exit scheme, delete the exit-code table in `unifi-gate.sh`
  and the report parsing in `unifi-reconcile.sh`. The Ansible checkout moved
  to `~/joy/tech/ansible` on 2026-08-15.

Done when a cron chain run on main is green three times in a row, the parked
in-sync scenario passes against the tagged provider fix, and the nightly
Ansible reconcile runs on 0.10.x.

## Phase 1: move the pins

One pull request. It does not wait for the provider fix: the read-only
scenarios run on the new images today, and the parked scenario stays parked.

- `tests/controllertest/pins.py`: `NETWORK_VERSION`, `UOS_VERSION`, both
  build notes, all four digests, `EMU_VERSION`, `PROVIDER_VERSION`.
- `.woodpecker/controller.yml` and `.github/workflows/controller-tests.yml`:
  image references, `UNIFI_TEST_EXPECT_VERSION`,
  `UNIFI_TEST_UOS_EXPECT_VERSION`, the `install-herder@` tag.
- `docs/provider-import-bugs.md`: record the 0.109.0 result above, correct
  the claim that `"24h0m0s"` and `"default"` are live values, and narrow the
  document to the two defaulted attributes, the absent `enabled` key, and the
  DHCP range case.
- Remove the `UNIFI_TEST_UOS_SEEDED_KEY` override and the 5.1.21 transcript
  from `tests/controllertest/uos.py`.
- Update the skip reason on the in-sync reconcile scenario to name the
  0.109.0 result.

`test_pins.py` already fails if any of those drift from each other, so the
change is mechanical.

## Phase 2: a coverage gate that separates "cannot model" from "would lose"

The fail-closed rule says generation writes nothing while any live field lacks
a provider attribute. On a fresh site that includes the controller's SSH
credentials under `mgmt.x_ssh_*`, ten `igmp_snooping` internals, and every
section the provider has not reached. Under that rule, adoption of any site
waits for the provider to model all of `get/setting`, which is not the
provider's goal.

Three options, in order of preference:

1. **Operator-owned accepted gaps.** A configuration list of section and field
   identifiers the operator accepts as unmanaged. Each accepted gap appears in
   the receipt with its identifier, so the acceptance is reviewable. The
   structural exclusions in `manifest.py` stay as they are.
2. **Default-aware gaps.** A gap whose live value equals the controller's own
   default cannot lose anything. The source for that default is the
   provider's management contract once it publishes one. Until then the only
   provider-side witness is a plan, and this option cannot be built without
   measuring defaults in ubitofu, which is the provider's job.
3. **Keep fail-closed and wait for provider coverage.** Cheapest, and it keeps
   every write scenario blocked indefinitely.

Recommendation: option 1 now, option 2 when the artifact is stable, and keep
the rule that an unmapped field with a non-default live value blocks.

## Phase 3: one helper layer for values and paths

Seven modules carry their own `_thaw`, four their own `_thaw_object`, and
`_remove_path`, `_truthy_paths`, `_canonical_json`, `_sha`, `_write_all`,
`_fsync_directory`, `_reject_json_constant`, and `_object_without_duplicates`
each exist two to four times. `values.py` is 35 lines and owns none of them.

Move freeze, thaw, path get and remove, truthy-path collection, canonical JSON,
digesting, and fsync-safe writes into `values.py` and an `io` helper, delete
the copies, and keep the mutation gate on the new module. This is a
behaviour-preserving move with the existing 624 unit tests as the net.

## Phase 4: shrink the transaction and receipt layers

`file_transaction.py` is 1,543 lines with 75 functions. It persists three
documents, a manifest, a journal, and a cleanup marker, each with a
hand-written encoder and decoder. `outcomes.py` is 1,151 lines and hand-writes
the receipt codec and its validators. `runtime.py` hand-writes a fourth
manifest.

- Replace the four hand-written codecs with one typed dataclass-to-JSON codec
  and one validator. Recovery behaviour is already pinned by 63 tests across
  `test_file_transaction.py`, `test_transaction_recovery.py`, and
  `test_runtime.py`, so the target is fewer lines with those tests unchanged.
- Collapse the three reason vocabularies in `reconcile_model.py`, 42
  `ReasonCode` members plus two smaller enums for the same concepts, into one
  vocabulary and a disposition mapping.
- Decide the future of the 810-line staged-validation proof harness in
  `tools/`. It proved a bounded contract on two platforms and is documented
  in `docs/staged-validation-contract.md`. If it is not going to run in CI it
  should be a fixture, not a tool.

## Phase 5: decide the contract branches

`emdash/provider-contract-0.10.1` binds one provider binary, CLI, schema, and
a shipped DNS differential corpus to a config-declared checksum.
`emdash/catalog-contract-parity` verifies provider catalog evidence. Both are
a hand-maintained copy of knowledge the provider now generates: served
schema snapshots pinned in its own CI, and behaviour it derives from the
SDK's measurements.

Recommendation: do not merge either branch. Keep them as reference, and
re-derive the admission check at cycle end from the provider alone: the
registry-served version, `tofu providers schema -json`, and the management
contract when the provider publishes it. The provider's own roadmap places
ubitofu downstream and optional, consuming provider contracts in shadow mode
only after their first fully measured resource, which matches this.

## Phase 6: controller suite

- Unblock the write scenarios (in-sync reconcile, drift capture, adopt new,
  stage deleted) once Phase 2 lands. The scenario list is in
  `docs/superpowers/plans/2026-07-19-container-controller-testing.md`.
- Re-check the v2 boot race (`firewall-policies` answering 500 past 90
  seconds) on 10.6.101. The images now gate readiness on v2 settling, so the
  race may be gone. Repeat-run the deleted-device scenario before deleting
  its guard, as the LTE case taught.
- Woodpecker cannot run the UOS or herder scenarios because its rootless
  agent has no Docker socket. unifi-containers moved its integration suite to
  Forgejo Actions runners. Either follow, or keep the GitHub Actions dispatch
  as the only home for those two markers and say so in `docs/testing.md`.
- Read the bundled UOS app version from the OCI label once the next
  unifi-containers release ships it, and retire the
  `UNIFI_TEST_UOS_EXPECT_VERSION` knob.

## Phase 7: derive more, maintain less

The rule for every item here: derive from the provider, never from the SDK
or the controller directly. The provider is the layer that turns controller
facts into Terraform semantics, and ubitofu reasons about those semantics.

- Secrets. `secrets.py` carries a hand-written table of two rules and
  `controller_projection.py` guesses secret-shaped fields by name. The served
  schema's `sensitive` and `write_only` flags are the provider's statement of
  what is secret, and the provider is deriving those flags from the
  controller's own declaration. Make the flags the only rule for HCL, and let
  the name heuristic disappear with the comparability decision in Phase 0,
  which removes the raw controller record from comparison.
- Provider defaults. `tofu providers schema -json` cannot show defaults, which
  is why provider-default drift needed a plan to detect. The plan stays the
  witness until the provider's management contract states defaults and
  ownership per attribute. When it does, use it to explain a
  `computed_or_unknown` outcome instead of reporting it bare.
- Coverage. Setting sections are already discovered from the served schema,
  so a provider tag bump needs no manifest change. Keep it that way for new
  resources by deriving the endpoint-to-resource table from the schema where
  the provider names the endpoint.

## Not on this roadmap

- Pinning main of any upstream, or anticipating unreleased resources such as
  `unifi_schedule_task`, `unifi_nat`, or the four unreleased setting sections.
- A second implementation of anything the 0.10 cutover removed.
- Refactoring Phases 3 and 4 before Phase 0 gives the code a production user.
  A regression in a refactor is invisible until something runs the result.
