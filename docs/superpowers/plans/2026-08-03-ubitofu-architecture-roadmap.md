# ubitofu Architecture Implementation Roadmap

Date: 2026-08-03
Status: implementation-ready planning set

This roadmap turns the approved architecture in
`docs/superpowers/specs/2026-08-02-ubitofu-reconciliation-architecture-design.md`
into six independently executable phases. This dated planning set is the active
implementation path; earlier monolithic drafts are design history.

## Durable objective

ubitofu remains a Python, plan-only Unix tool. It generates maintainable HCL
from a live UniFi controller, captures later UI or mobile-app changes into
reviewable HCL, and permits HCL and controller workflows to coexist without
silently choosing a winner when both changed the same managed value.

The implementation is an internal hardening, not a rewrite. It does not add
controller mutation, `tofu apply`, state mutation, Git/forge behavior, CI-vendor
behavior, state backup, notifications, or deployment-secret management.

## Phase order

| Phase | Plan | Deliverable | Depends on |
| --- | --- | --- | --- |
| 1 | `2026-08-03-01-safety-foundation.md` | strict adapters, private artifacts, lock, safe diagnostics, controller policy | approved design and completed proofs |
| 2 | `2026-08-03-02-semantic-planner.md` | immutable snapshots and total pure three-way planner | phase 1 |
| 3 | `2026-08-03-03-transactional-editing.md` | in-memory rendering, offline staged validation, durable multi-file recovery | phases 1-2 |
| 4 | `2026-08-03-04-structural-hcl-index.md` | tree-sitter HCL index and anchored byte-span editing | phase 3 |
| 5 | `2026-08-03-05-public-interfaces.md` | saved-plan check, inspect, health, receipts, compatibility surface | phases 1-4 |
| 6 | `2026-08-03-06-ansible-cutover.md` | dual-run evidence and thin Ansible consumer cutover | phase 5 released |

The order is a safety property:

- The planner cannot consume raw mutable OpenTofu or controller documents, so
  strict adapters land first.
- Transactional rendering needs one immutable `ReconcilePlan`, so it follows
  the planner.
- Parser replacement changes where edits land, so it follows transaction
  protection even though its feasibility proof is already complete.
- Public automation contracts should expose the stable core, not an
  intermediate implementation.
- Ansible cuts over only to released public interfaces and retains its own
  deployment and publication policy.

## Phase gates

Each phase is independently releasable except the explicitly breaking TLS task
in phase 1. Do not begin a later phase until the previous phase's completion
gate is green and its commits have received review.

### Gate 1: trustworthy boundaries

- Plan results are accepted only for exit 0/2 and `errored: false`.
- Non-plan OpenTofu commands require exit 0.
- Scratch artifacts are private, cleaned on handled exits, and reclaimed after
  process death under a workdir lock.
- Normal diagnostics expose allowlisted context rather than raw external data.
- TLS verification and endpoint-response policy ship together in a declared
  breaking release.

### Gate 2: deterministic semantics

- The planner is a total pure function over deep-frozen snapshots.
- Every observation has exactly one ordered decision.
- Compatible changes merge; same-field divergent changes conflict.
- Unsupported addresses and unstable collection identities become attention,
  never guessed edits.
- Human categories and outcome codes derive from decisions.

### Gate 3: no partial worktree

- Check and wet mode build equal plans and candidate bytes.
- All candidates validate in the proven offline staged module.
- Success yields the complete new tree.
- A failed commit with successful rollback yields the complete original tree.
- Ambiguous or failed recovery quarantines backups and blocks mutation.
- Process-kill tests cover every durable boundary.

### Gate 4: structural source safety

- Runtime tree-sitter versions match the proof.
- Every `ERROR` or `MISSING` node fails closed.
- The production index passes the 53-span corpus and current reconcile fixtures.
- No-op editing is byte-identical; disjoint patches are order-independent.
- Native and JSON configuration honor OpenTofu file-precedence rules.

### Gate 5: stable Unix interfaces

- `check` inspects exactly the supplied saved plan and emits its digest.
- `inspect` and `health` replace consumer-side provider/controller parsing.
- Human and versioned JSON output derive from the same outcome.
- Existing commands remain compatibility entry points for one major cycle.
- No CI-vendor or deployment behavior enters the package.

### Gate 6: consumer parity

- Old and new Ansible paths agree on the defined scenario matrix.
- The same plan file checked by ubitofu is applied externally.
- State backup, apply, Git publication, Woodpecker routing, PR creation, and
  notification remain in the Ansible repository.
- Shell policy is removed only after its replacement command has parity
  evidence and a rollback path.

## Verification lanes

Every phase runs the default unit, property, lint, type, and branch-coverage
gates. Additional lanes are cumulative:

- real OpenTofu adapter tests after phase 1;
- planner table and mutation tests after phase 2;
- macOS/Linux staged validation and process-kill recovery after phase 3;
- native-wheel packaging and HCL corpus tests after phase 4;
- CLI/receipt compatibility and selected controller scenarios after phase 5;
- consumer dual-run pipelines after phase 6.

The per-PR mutation gate grows with the new safety modules. `pipeline.py` leaves
mutation scope only after it is mechanically checked as a thin composition
root; branch coverage remains required.

## Implementation handoff

Implement one phase per branch or stacked pull request. Within a phase, follow
the task commit boundaries in the named plan. Use test-driven development and
request an adversarial review at each phase gate. Do not combine phase 6's
private consumer changes into the public ubitofu repository history.
