# ubitofu Semantic Planner Implementation Plan

Status: superseded by `2026-08-03-ubitofu-0.10-single-cutover.md`. Retained as
review history. Do not execute this phase independently.

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> superpowers:subagent-driven-development (recommended) or
> superpowers:executing-plans to implement this plan task-by-task. Steps use
> checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace reconciliation classification inside `pipeline.py` with a
total, deterministic three-way planner over immutable snapshots.

**Architecture:** External adapters validate and deep-freeze controller,
OpenTofu, schema, and committed-source inputs. A pure planner maps each complete
resource observation to one ordered decision and anchored edit intents. A
renderer computes every candidate byte string before the existing final write
loop. Phase 3 replaces that write loop with a transaction.

**Tech Stack:** Python 3.11+, frozen dataclasses, enums, recursive immutable
value objects, pytest parameterization, Hypothesis, Ruff, strict mypy, mutmut.

## Global Constraints

- Phase 1 must be complete. Do not weaken its workdir lock, artifact ownership,
  safe diagnostics, TLS, or adapter validation.
- The planner has no filesystem, network, subprocess, clock, environment,
  randomness, logging, or output-stream dependency.
- Do not pass `dict`, `list`, `set`, provider-schema mappings, or controller
  payloads into the planner. Freeze at the boundary.
- Preserve the complete OpenTofu address. Never collapse module, index, data,
  or deposed identity onto a root `type.name` edit target.
- Compatible HCL/controller changes merge. Different changes to the same
  managed value conflict. Neither authoring surface has priority.
- Provider-computed values and planned unknowns do not become drift.
- Positional-list merging is forbidden without schema-backed stable identities.
- Reports and exit codes derive from ordered decisions; do not maintain parallel
  category lists or a separately stored outcome code.
- The temporary phase-2 write loop is not transactional and must be clearly
  marked for replacement by phase 3. It may run only after all candidate bytes
  have been rendered successfully.

---

### Task 1: Model the exact immutable OpenTofu subset

**Files:**

- Create: `src/ubitofu/reconcile_model.py`
- Expand: `src/ubitofu/tofu_json.py`
- Modify: `src/ubitofu/tofu_runner.py`
- Expand: `tests/test_tofu_json.py`
- Modify: `tests/test_tofu_runner.py`

**Interfaces:**

```python
Scalar = None | bool | int | float | str

@dataclass(frozen=True)
class FrozenObject:
    items: tuple[tuple[str, "FrozenValue"], ...]

FrozenValue = Scalar | tuple["FrozenValue", ...] | FrozenObject

@dataclass(frozen=True, order=True)
class OpenTofuAddress:
    absolute: str
    module: str | None
    mode: Literal["managed", "data"]
    resource_type: str
    name: str
    index: str | int | None
    deposed: str | None

    @property
    def editable(self) -> bool: ...

class ActionVector(Enum):
    NOOP = ("no-op",)
    CREATE = ("create",)
    READ = ("read",)
    UPDATE = ("update",)
    DELETE = ("delete",)
    FORGET = ("forget",)
    DELETE_CREATE = ("delete", "create")
    CREATE_DELETE = ("create", "delete")

@dataclass(frozen=True)
class ResourceChange:
    address: OpenTofuAddress
    action: ActionVector
    before: FrozenObject | None
    after: FrozenObject | None
    after_unknown: FrozenObject

@dataclass(frozen=True)
class PlanDocument:
    format_version: tuple[int, int]
    changes: tuple[ResourceChange, ...]

def parse_plan_document(value: object) -> PlanDocument: ...
def parse_state_document(value: object) -> StateDocument: ...
def parse_provider_schema(value: object) -> ProviderSchema: ...
```

- [ ] Add tests for all eight action vectors; nullable before/after shapes;
  root, module, indexed, data-mode, and deposed addresses; numeric/string
  indices; malformed address fields; malformed action arrays; stable input
  ordering; and unknown minor fields.

- [ ] Add recursive immutability tests. Attempts to mutate retained maps,
  sequences, `before`, `after`, `after_unknown`, and provider blocks must fail.
  Mutating the original decoded JSON after parsing must not affect the model.

- [ ] Add state and provider-schema tests only for fields current consumers use.
  Missing required used fields report their logical field path. Unknown fields
  are ignored for minor-version compatibility.

- [ ] Confirm red:

  ```bash
  python3 -m pytest tests/test_tofu_json.py tests/test_tofu_runner.py -q
  ```

- [ ] Implement canonical key ordering inside `FrozenObject`. Validate value
  kinds recursively and reject non-string object keys, NaN, and infinities.
  Retain the opaque absolute address alongside parsed fields.

- [ ] Define `editable` as root-module, managed, unindexed, current/non-deposed
  only. Legal but non-editable addresses remain observations; they are not
  parser errors.

- [ ] Verify:

  ```bash
  python3 -m pytest tests/test_tofu_json.py tests/test_tofu_runner.py \
    tests/test_pipeline.py tests/test_coverage.py -q
  python3 -m ruff check src/ubitofu/reconcile_model.py \
    src/ubitofu/tofu_json.py tests/test_tofu_json.py
  python3 -m mypy src/ubitofu
  ```

- [ ] Commit:

  ```bash
  git add src/ubitofu/reconcile_model.py src/ubitofu/tofu_json.py \
    src/ubitofu/tofu_runner.py tests/test_tofu_json.py \
    tests/test_tofu_runner.py tests/test_pipeline.py tests/test_coverage.py
  git commit -m "tofu: normalize immutable plan state and schema models"
  ```

---

### Task 2: Build complete normalized snapshots before planning

**Files:**

- Create: `src/ubitofu/reconcile_snapshot.py`
- Modify: `src/ubitofu/pipeline.py`
- Modify: `src/ubitofu/enumerator.py`
- Create: `tests/test_reconcile_snapshot.py`
- Modify: `tests/test_reconcile.py`
- Modify: `tests/test_properties.py`

**Interfaces:**

```python
@dataclass(frozen=True)
class FileIdentity:
    relative_path: PurePosixPath
    device: int
    inode: int
    mode: int
    size: int
    mtime_ns: int
    sha256: str

@dataclass(frozen=True)
class SourceResource:
    address: OpenTofuAddress
    file: FileIdentity
    block_bytes: bytes
    attributes: FrozenObject
    attribute_literals: tuple[tuple[tuple[str | int, ...], bytes], ...]

@dataclass(frozen=True)
class ResourceObservation:
    address: OpenTofuAddress
    committed: SourceResource | None
    prior_state: FrozenObject | None
    live: FrozenObject | None
    change: ResourceChange | None
    lifecycle: LifecyclePolicy
    stable_collection_keys: tuple[tuple[tuple[str | int, ...], str], ...]

@dataclass(frozen=True)
class ReconcileSnapshot:
    resources: tuple[ResourceObservation, ...]
    source_files: tuple[SourceFile, ...]
    declarations: DeclarationSnapshot

def collect_reconcile_snapshot(
    *,
    controller: Controller,
    runner: TofuRunner,
    workdir: Path,
    site: str,
) -> ReconcileSnapshot: ...
```

- [ ] Add tests proving all external reads finish before the collector returns
  and that snapshot collection performs zero persistent writes. Inject a late
  controller/schema/state/source failure and assert destination hashes unchanged.

- [ ] Characterize current identities, lifecycle fields, sensitive attributes,
  declared complex attributes, imports, variables, unknown paths, committed
  resources, state-only resources, live-only resources, and controller ordering.

- [ ] Add file identity tests for atomic-editor replacement, metadata-only
  changes, symlinks, duplicate addresses, non-regular files, and files changed
  during collection. Source paths must be workdir-relative and non-escaping.

- [ ] Confirm red:

  ```bash
  python3 -m pytest tests/test_reconcile_snapshot.py tests/test_reconcile.py \
    tests/test_properties.py -q
  ```

- [ ] Move schema-dependent cleaning, identity extraction, lifecycle extraction,
  sensitive-field classification, and planned-unknown normalization into the
  collector. Sort observations by `OpenTofuAddress.absolute`.

- [ ] Keep current lexical HCL discovery behind a private snapshot adapter for
  this phase. Record that phase 4 replaces it; do not expand the handwritten
  scanner.

- [ ] Verify:

  ```bash
  python3 -m pytest tests/test_reconcile_snapshot.py tests/test_reconcile.py \
    tests/test_pipeline.py tests/test_properties.py -q
  python3 -m ruff check src/ubitofu/reconcile_snapshot.py \
    tests/test_reconcile_snapshot.py
  python3 -m mypy src/ubitofu
  ```

- [ ] Commit:

  ```bash
  git add src/ubitofu/reconcile_snapshot.py src/ubitofu/pipeline.py \
    src/ubitofu/enumerator.py tests/test_reconcile_snapshot.py \
    tests/test_reconcile.py tests/test_properties.py
  git commit -m "reconcile: collect complete immutable input snapshots"
  ```

---

### Task 3: Define decisions and the total three-way merge

**Files:**

- Expand: `src/ubitofu/reconcile_model.py`
- Create: `src/ubitofu/reconcile_planner.py`
- Create: `tests/test_reconcile_planner.py`
- Modify: `tests/test_properties.py`
- Modify: `tests/test_reconcile.py`

**Interfaces:**

```python
class Disposition(Enum):
    NO_CHANGE = "no_change"
    PRESERVE_CODE = "preserve_code"
    CAPTURE_LIVE = "capture_live"
    APPEND = "append"
    REMOVE = "remove"
    CONFLICT = "conflict"
    ATTENTION = "attention"
    FORBIDDEN = "forbidden"

class ReasonCode(Enum):
    NO_CHANGE = "no_change"
    CODE_ONLY_CHANGE = "code_only_change"
    LIVE_ONLY_CHANGE = "live_only_change"
    CONCURRENT_CHANGE_CONVERGED = "concurrent_change_converged"
    CONCURRENT_VALUE_CONFLICT = "concurrent_value_conflict"
    COMPUTED_OR_UNKNOWN = "computed_or_unknown"
    SECRET_SUPPRESSED = "secret_suppressed"
    LIVE_RESOURCE_NEW = "live_resource_new"
    CONTROLLER_RESOURCE_DELETED = "controller_resource_deleted"
    PENDING_CREATE = "pending_create"
    PENDING_DELETE = "pending_delete"
    STATE_ORPHANED = "state_orphaned"
    COMMITTED_NOT_IN_STATE = "committed_not_in_state"
    FORBIDDEN_DEVICE_CREATE = "forbidden_device_create"
    UNSUPPORTED_ADDRESS = "unsupported_address"
    UNSTABLE_COLLECTION_IDENTITY = "unstable_collection_identity"
    DELETE_MODIFY_CONFLICT = "delete_modify_conflict"
    REPLACEMENT_REQUIRES_ATTENTION = "replacement_requires_attention"
    FORGET_REQUIRES_ATTENTION = "forget_requires_attention"
    JSON_SOURCE_READ_ONLY = "json_source_read_only"
    DANGLING_REFERENCE = "dangling_reference"
    DECLARED_COMPLEX_DRIFT = "declared_complex_drift"

@dataclass(frozen=True)
class SourceAnchor:
    address: OpenTofuAddress
    attribute_path: tuple[str | int, ...] | None
    expected_literal: bytes

@dataclass(frozen=True)
class UpdateScalar:
    anchor: SourceAnchor
    replacement_literal: bytes

@dataclass(frozen=True)
class DeleteResource:
    anchor: SourceAnchor

@dataclass(frozen=True)
class AppendResource:
    address: OpenTofuAddress
    hcl: bytes

@dataclass(frozen=True)
class AppendImport:
    address: OpenTofuAddress
    import_id: str

@dataclass(frozen=True)
class DeclareVariable:
    name: str
    sensitive: bool

EditIntent = UpdateScalar | DeleteResource | AppendResource | AppendImport | DeclareVariable

@dataclass(frozen=True)
class AttentionItem:
    reason: ReasonCode
    address: OpenTofuAddress | None
    message: str

@dataclass(frozen=True)
class ResourceDecision:
    address: OpenTofuAddress
    disposition: Disposition
    reason: ReasonCode
    edits: tuple[EditIntent, ...]
    attention: tuple[AttentionItem, ...]

@dataclass(frozen=True)
class ReconcilePlan:
    decisions: tuple[ResourceDecision, ...]
    global_edits: tuple[EditIntent, ...]

def build_reconcile_plan(snapshot: ReconcileSnapshot) -> ReconcilePlan: ...
```

- [ ] Write the attribute three-way table first. For base/state `B`, committed
  HCL `H`, and live `L`, cover: all equal; code-only; live-only; equal concurrent
  result; different concurrent result; unknown/computed; absent versus null; and
  secret suppression. Live-only produces an anchored edit. Code-only preserves
  HCL. Different concurrent results conflict with no edit.

- [ ] Write the resource-existence table for present/absent committed, state,
  and live values plus exact plan action. Cover deleted, pending, orphaned,
  codified, new, replacement, forget, forbidden device create, module/index/data/
  deposed attention, delete-versus-modify, and expanded-address shared-block
  deletion safety.

- [ ] Write stable-collection tests. Keyed map/set elements may merge by key.
  Positional lists, duplicate keys, and unstable identities produce attention or
  conflict. Never infer list identity from position.

- [ ] Add Hypothesis properties for determinism, input-order independence, one
  decision per observation, unique edit anchors, forbidden-not-pending,
  deep immutability, and absence of check/wet mode from planner inputs.

- [ ] Confirm red:

  ```bash
  python3 -m pytest tests/test_reconcile_planner.py tests/test_properties.py \
    tests/test_reconcile.py -q
  ```

- [ ] Implement a total dispatcher. Every supported combination returns one
  decision. Impossible internal combinations raise `InvalidSnapshot`; legal but
  unsupported combinations return `ATTENTION`. Do not use a catch-all no-op.
  The `ReasonCode` values above are the initial machine contract; any additional
  table row must add an explicit enum member and receipt-compatibility test.

- [ ] Derive category counts and exits from decisions with one pure function:

  ```python
  def outcome_for(plan: ReconcilePlan) -> ReconcileOutcome: ...
  ```

  Keep 0/10/11/12/13 compatibility. A forbidden device create dominates other
  outcome categories as current behavior requires.

- [ ] Verify:

  ```bash
  python3 -m pytest tests/test_reconcile_planner.py tests/test_properties.py \
    tests/test_reconcile.py tests/test_reporter.py -q
  python3 -m ruff check src/ubitofu/reconcile_model.py \
    src/ubitofu/reconcile_planner.py tests/test_reconcile_planner.py
  python3 -m mypy src/ubitofu
  ```

- [ ] Commit:

  ```bash
  git add src/ubitofu/reconcile_model.py src/ubitofu/reconcile_planner.py \
    tests/test_reconcile_planner.py tests/test_properties.py \
    tests/test_reconcile.py tests/test_reporter.py
  git commit -m "reconcile: classify immutable observations with a total planner"
  ```

---

### Task 4: Render every candidate before the first write

**Files:**

- Create: `src/ubitofu/reconcile_renderer.py`
- Modify: `src/ubitofu/reporter.py`
- Modify: `src/ubitofu/pipeline.py`
- Create: `tests/test_reconcile_renderer.py`
- Modify: `tests/test_reporter.py`
- Modify: `tests/test_pipeline.py`
- Modify: `tests/test_reconcile.py`

**Interfaces:**

```python
@dataclass(frozen=True)
class ProposedFile:
    relative_path: PurePosixPath
    original_sha256: str | None
    original_mode: int | None
    candidate: bytes | None  # None means delete

def render_candidates(
    plan: ReconcilePlan,
    snapshot: ReconcileSnapshot,
) -> tuple[ProposedFile, ...]: ...

def render_reconcile_report(plan: ReconcilePlan) -> str: ...
```

- [ ] Add golden compatibility tests for no-op, scalar capture, complex
  attention, removal, pending, orphaned, codified, new resource, secret
  variable, dangling reference, coverage, conflict, and combined outcomes.
  Capture report text, exit code, and candidate bytes.

- [ ] Test duplicate destinations, duplicate or missing anchors, expected-old
  mismatch, overlapping edits, render failure after several files, deletion,
  creation, and no-op byte identity. Any render failure yields no writes.

- [ ] Confirm red:

  ```bash
  python3 -m pytest tests/test_reconcile_renderer.py tests/test_reporter.py \
    tests/test_pipeline.py tests/test_reconcile.py -q
  ```

- [ ] Implement rendering against snapshot bytes. Normalize edits per file,
  reject overlap, verify expected literals, and apply from the highest byte
  offset down. Continue using current `hcl_surgeon` locating behavior only
  through this isolated adapter until phase 4.

- [ ] Reduce `run_reconcile()` to collect snapshot, build plan, render every
  candidate, write candidates only when `check=False`, and render the report
  from the plan. Add an assertion test that check and wet runs given the same
  snapshot produce equal plan/candidate tuples.

- [ ] Keep the final direct-write loop in one private function named
  `_commit_candidates_legacy`. Document that phase 3 deletes it. It must be the
  only persistent write site in reconcile after this task.

- [ ] Verify:

  ```bash
  python3 -m pytest tests/test_reconcile_renderer.py tests/test_reporter.py \
    tests/test_pipeline.py tests/test_reconcile.py tests/test_properties.py -q
  python3 -m ruff check src tests
  python3 -m mypy src/ubitofu
  git diff --check
  ```

- [ ] Commit:

  ```bash
  git add src/ubitofu/reconcile_renderer.py src/ubitofu/reporter.py \
    src/ubitofu/pipeline.py tests/test_reconcile_renderer.py \
    tests/test_reporter.py tests/test_pipeline.py tests/test_reconcile.py
  git commit -m "reconcile: render ordered decisions before writing files"
  ```

---

### Task 5: Mutation-gate planner semantics

**Files:**

- Modify: `pyproject.toml`
- Modify: `ci/mutation_gate.py`
- Modify: `.woodpecker/ci.yml`
- Modify: `tests/test_infra_deps.py`
- Modify: `docs/testing.md`

- [ ] Add `tofu_json.py`, `reconcile_snapshot.py`, `reconcile_planner.py`, and
  `reconcile_renderer.py` to the per-PR and weekly mutation maps and matching
  Woodpecker path filters.

- [ ] Run module-by-module mutation checks. Add behavior tests for meaningful
  survivors, especially equality direction, action-vector order, unknown
  suppression, conflict versus merge, and decision-to-exit mapping.

- [ ] Measure `pipeline.py` complexity. It remains in weekly mutation scope
  until phase 3 removes `_commit_candidates_legacy` and an objective threshold
  shows it is a thin composition root.

- [ ] Run the phase gate:

  ```bash
  python3 -m pytest -m "not controller" --cov=ubitofu --cov-branch
  python3 -m ruff check .
  python3 -m mypy src
  python3 ci/mutation_gate.py check
  git diff --check
  ```

- [ ] Request an adversarial review focused on table completeness, immutable
  boundaries, address identity, keyed deletion, concurrent change semantics,
  unknown/computed values, and report/exit derivation.

- [ ] Commit:

  ```bash
  git add pyproject.toml ci/mutation_gate.py .woodpecker/ci.yml \
    tests/test_infra_deps.py docs/testing.md
  git commit -m "ci: mutation-test semantic reconciliation decisions"
  ```

## Completion Gate

Phase 2 is complete only when the complete decision table and properties pass,
planner inputs are demonstrably deep-frozen, check/wet plans are equal, all
candidate bytes exist before the only legacy write loop starts, reports and
outcomes derive solely from decisions, and planner mutations have no unexplained
survivors. Do not claim multi-file atomicity; phase 3 provides recovery.
