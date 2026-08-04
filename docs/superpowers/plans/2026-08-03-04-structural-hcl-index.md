# ubitofu Structural HCL Index Implementation Plan

Status: superseded by `2026-08-03-ubitofu-0.10-single-cutover.md`. Retained as
review history. Do not execute this phase independently.

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> superpowers:subagent-driven-development (recommended) or
> superpowers:executing-plans to implement this plan task-by-task. Steps use
> checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace handwritten lexical discovery with the proved tree-sitter HCL
index while preserving operator-owned bytes outside explicit edit spans.

**Architecture:** A strict parser adapter turns original UTF-8 bytes into an
immutable structural index of blocks, attributes, and qualified references.
Every recovered `ERROR` or `MISSING` node fails closed. Reconciliation converts
indexed anchors into verified, non-overlapping byte patches and applies them
from the highest offset downward inside the phase-3 transaction. Strict JSON
discovery handles HCL JSON configuration, which remains read-only initially.

**Tech Stack:** Python 3.11+, `tree-sitter==0.26.0`,
`tree-sitter-hcl==1.2.0`, strict JSON, original-byte slicing, pytest corpus,
Hypothesis, Ruff, strict mypy, mutmut, wheel/sdist packaging tests.

## Global Constraints

- Phase 3 must be complete. The parser proof selected the engine but did not
  authorize production edits before transaction protection.
- Use the proof's exact dependency versions initially. Upgrade them only through
  a new corpus and packaging result.
- Reject UTF-8 BOM, invalid UTF-8, bare carriage returns where the parser cannot
  preserve coordinates, and every tree-sitter recovery node.
- Never parse and re-emit operator-owned HCL. Original bytes remain the source
  of truth.
- No-op returns byte-identical output. A patch changes only `[start:end]` after
  matching its expected bytes.
- Duplicate addresses, ambiguous anchors, overlapping patches, out-of-bounds
  spans, and expected-byte mismatches fail before transaction commit.
- `.tf.json` and `.tofu.json` resources participate in discovery but are not
  byte-edited in this release. Edit-required drift becomes a stable attention
  result.
- Honor OpenTofu native/JSON, `.tofu`/`.tf`, and override precedence so a
  shadowed source is never an active edit target.
- Retain `python-hcl2` for ubitofu-owned HCL generation; this phase replaces
  source indexing, not the existing writer.

---

### Task 1: Add pinned runtime and packaging coverage

**Files:**

- Modify: `pyproject.toml`
- Modify: `tests/test_infra_deps.py`
- Create: `tests/test_packaging.py`
- Modify: `.github/workflows/ci.yml`
- Modify: `.woodpecker/ci.yml`

- [ ] Add failing infrastructure tests requiring exact runtime pins
  `tree-sitter==0.26.0` and `tree-sitter-hcl==1.2.0`. Exact pins preserve the
  parser ABI and proof result; looser ranges are not accepted in this phase.

- [ ] Add a wheel/sdist smoke test that installs the built artifact into a clean
  environment and imports both parser distributions plus `ubitofu`. It must not
  rely on the source checkout being importable.

- [ ] Add Linux x86_64/arm64 and macOS arm64 packaging lanes where available.
  A missing wheel that triggers an unplanned local compiler requirement is a
  release blocker, not a skipped test.

- [ ] Confirm red:

  ```bash
  python3 -m pytest tests/test_infra_deps.py tests/test_packaging.py -q
  ```

- [ ] Add the exact dependencies and update CI. Do not remove `python-hcl2`,
  which `hcl_writer.py` still uses.

- [ ] Verify:

  ```bash
  python3 -m pytest tests/test_infra_deps.py tests/test_packaging.py -q
  python3 -m build
  python3 -m ruff check .
  python3 -m mypy src
  ```

- [ ] Commit:

  ```bash
  git add pyproject.toml tests/test_infra_deps.py tests/test_packaging.py \
    .github/workflows/ci.yml .woodpecker/ci.yml
  git commit -m "build: ship the proved tree-sitter HCL runtime"
  ```

---

### Task 2: Implement the strict immutable HCL index

**Files:**

- Create: `src/ubitofu/hcl_index.py`
- Create: `tests/test_hcl_index.py`
- Reuse: `tests/fixtures/hcl/corpus.json`
- Reuse: `tests/fixtures/hcl/structural_edge_cases.tf`
- Reference: `tools/hcl_parser_proof.py`
- Reference: `docs/hcl-parser-decision.md`

**Interfaces:**

```python
@dataclass(frozen=True, order=True)
class ByteSpan:
    start: int
    end: int

@dataclass(frozen=True, order=True)
class BlockKey:
    kind: str
    labels: tuple[str, ...]
    parent: tuple[tuple[str, tuple[str, ...]], ...]

@dataclass(frozen=True)
class BlockSpan:
    key: BlockKey
    whole: ByteSpan
    body: ByteSpan

@dataclass(frozen=True)
class AttributeSpan:
    block: BlockKey
    name: str
    whole: ByteSpan
    expression: ByteSpan

@dataclass(frozen=True)
class ReferenceSpan:
    attribute: tuple[BlockKey, str]
    traversal: tuple[str | int, ...]
    expression: ByteSpan

@dataclass(frozen=True)
class HclIndex:
    source_sha256: str
    blocks: tuple[BlockSpan, ...]
    attributes: tuple[AttributeSpan, ...]
    references: tuple[ReferenceSpan, ...]

    def resource(self, resource_type: str, name: str) -> BlockSpan: ...
    def attribute(self, block: BlockKey, name: str) -> AttributeSpan: ...

def index_hcl(*, path: PurePosixPath, source: bytes) -> HclIndex: ...
```

- [ ] Move the selected adapter expectations, not the disposable comparison
  code, into `tests/test_hcl_index.py`. Require all 8 valid cases, 3 invalid
  cases, 53 ordered literal spans, invalid UTF-8, BOM, heredoc/comment/string
  decoys, CRLF, mixed newlines, Unicode, no final newline, nested blocks,
  interpolation, and qualified references.

- [ ] Add API tests for exact block labels containing punctuation/dots, nested
  blocks with repeated names, duplicate top-level resource addresses, missing
  attributes, and source-hash mismatch. Dotted display strings are not keys.

- [ ] Confirm red:

  ```bash
  python3 -m pytest tests/test_hcl_index.py -q
  ```

- [ ] Implement raw-byte parsing with a complete tree walk that rejects every
  node whose type is `ERROR`/`MISSING` or whose `is_error`/`is_missing` flag is
  true. Decode only individual label/reference spans as strict UTF-8 after the
  whole source passes UTF-8 validation.

- [ ] Keep tuples in deterministic source order and separately reject duplicate
  active resource keys. Validate `0 <= start <= end <= len(source)` for every
  retained span.

- [ ] Verify:

  ```bash
  python3 -m pytest tests/test_hcl_index.py \
    proofs/test_hcl_parser_proof.py -q
  python3 -m ruff check src/ubitofu/hcl_index.py tests/test_hcl_index.py
  python3 -m mypy src/ubitofu
  ```

- [ ] Commit:

  ```bash
  git add src/ubitofu/hcl_index.py tests/test_hcl_index.py
  git commit -m "hcl: index strict original-byte source structure"
  ```

---

### Task 3: Normalize verified byte patches

**Files:**

- Create: `src/ubitofu/hcl_patches.py`
- Modify: `src/ubitofu/hcl_surgeon.py`
- Create: `tests/test_hcl_patches.py`
- Modify: `tests/test_hcl_surgeon.py`
- Modify: `tests/test_properties.py`

**Interfaces:**

```python
@dataclass(frozen=True)
class BytePatch:
    span: ByteSpan
    expected: bytes
    replacement: bytes
    reason: str

def normalize_patches(
    source: bytes,
    patches: Iterable[BytePatch],
) -> tuple[BytePatch, ...]: ...

def apply_patches(source: bytes, patches: Iterable[BytePatch]) -> bytes: ...
```

- [ ] Add tests for no patches, one update, resource deletion, insertion,
  disjoint edits, adjacent edits, overlap, duplicates, out-of-bounds spans,
  expected mismatch, invalid replacement bytes, and deterministic descending
  application.

- [ ] Add Hypothesis properties: no-op identity; all bytes outside selected
  spans are preserved; disjoint patch result is independent of input order; and
  applying a normalized patch set twice fails on expected bytes rather than
  silently corrupting source.

- [ ] Confirm red:

  ```bash
  python3 -m pytest tests/test_hcl_patches.py tests/test_properties.py -q
  ```

- [ ] Implement validation of every patch before applying any patch. Sort by
  `(start, end)` for overlap checks, then apply in descending start order.

- [ ] Reimplement the public compatibility functions in `hcl_surgeon.py` as
  thin string/byte wrappers over `index_hcl()` and `apply_patches()`. Remove
  `_skip_string`, `_skip_line_comment`, `_skip_block_comment`, `_match_brace`,
  `_locate`, and `_top_level_assignments` after their callers migrate.

- [ ] Preserve existing function results for valid fixtures, except the
  deliberate heredoc-decoy bug fix. Add a regression proving a fake resource in
  a heredoc cannot redirect update or deletion.

- [ ] Verify:

  ```bash
  python3 -m pytest tests/test_hcl_patches.py tests/test_hcl_surgeon.py \
    tests/test_properties.py -q
  python3 -m ruff check src/ubitofu/hcl_patches.py \
    src/ubitofu/hcl_surgeon.py tests/test_hcl_patches.py
  python3 -m mypy src/ubitofu
  ```

- [ ] Commit:

  ```bash
  git add src/ubitofu/hcl_patches.py src/ubitofu/hcl_surgeon.py \
    tests/test_hcl_patches.py tests/test_hcl_surgeon.py \
    tests/test_properties.py
  git commit -m "hcl: apply anchored non-overlapping byte patches"
  ```

---

### Task 4: Index the effective OpenTofu module

**Files:**

- Create: `src/ubitofu/module_index.py`
- Modify: `src/ubitofu/module_manifest.py`
- Create: `tests/test_module_index.py`
- Modify: `tests/test_module_manifest.py`
- Modify: `tests/test_reconcile_snapshot.py`

**Interfaces:**

```python
class SourceSyntax(Enum):
    NATIVE = "native"
    JSON = "json"

@dataclass(frozen=True)
class IndexedSource:
    relative_path: PurePosixPath
    syntax: SourceSyntax
    active: bool
    hcl: HclIndex | None
    json_value: FrozenObject | None

@dataclass(frozen=True)
class ModuleIndex:
    sources: tuple[IndexedSource, ...]
    resources: tuple[IndexedResource, ...]
    imports: tuple[IndexedImport, ...]
    variables: tuple[IndexedVariable, ...]
    references: tuple[IndexedReference, ...]

def index_effective_module(
    *,
    workdir: Path,
    candidates: tuple[ProposedFile, ...] = (),
) -> ModuleIndex: ...
```

- [ ] Add precedence tests separately for native and JSON syntax, `.tofu` over
  same-stem `.tf`, `override` and `_override` files, candidate shadowing, and
  deletion exposing a formerly shadowed source. Assert inactive files are never
  edit targets.

- [ ] Add strict JSON tests for resources, imports, and variables plus duplicate
  JSON keys, malformed JSON, wrong shapes, and native/JSON address collisions.
  JSON resources are classified but `editable=False`.

- [ ] Confirm red:

  ```bash
  python3 -m pytest tests/test_module_index.py tests/test_module_manifest.py \
    tests/test_reconcile_snapshot.py -q
  ```

- [ ] Implement the effective source set once in `module_index.py`; make
  `module_manifest.py` consume it rather than maintaining independent
  precedence logic. Preserve inactive-source metadata only for diagnostics.

- [ ] Build qualified-reference discovery from expression nodes. Exclude
  comments, quoted strings, and literal-only heredoc text while retaining
  interpolations. If a reference form is intentionally unsupported, return a
  typed attention item with a regression test.

- [ ] Verify:

  ```bash
  python3 -m pytest tests/test_module_index.py tests/test_module_manifest.py \
    tests/test_reconcile_snapshot.py -q
  python3 -m ruff check src/ubitofu/module_index.py tests/test_module_index.py
  python3 -m mypy src/ubitofu
  ```

- [ ] Commit:

  ```bash
  git add src/ubitofu/module_index.py src/ubitofu/module_manifest.py \
    tests/test_module_index.py tests/test_module_manifest.py \
    tests/test_reconcile_snapshot.py
  git commit -m "hcl: index the effective OpenTofu source set"
  ```

---

### Task 5: Route reconciliation discovery and edits through the index

**Files:**

- Modify: `src/ubitofu/reconcile_snapshot.py`
- Modify: `src/ubitofu/reconcile_renderer.py`
- Modify: `src/ubitofu/reconcile_planner.py`
- Modify: `src/ubitofu/pipeline.py`
- Modify: `tests/test_reconcile.py`
- Modify: `tests/test_reconcile_renderer.py`
- Modify: `tests/test_pipeline.py`

- [ ] Add end-to-end fixture tests for committed resources, imports, variables,
  declared repeated blocks, dangling references, scalar edits, resource
  deletion, new resources, heredoc decoys, duplicate addresses, JSON-owned
  resources, shadowed files, and mixed `.tf`/`.tofu` modules.

- [ ] Add a JSON-owned drift case that returns an attention decision and no
  patch. Add a native case whose qualified reference points at a resource being
  removed and verify the indexed dangling-reference diagnostic.

- [ ] Confirm red:

  ```bash
  python3 -m pytest tests/test_reconcile.py tests/test_reconcile_renderer.py \
    tests/test_pipeline.py -q
  ```

- [ ] Replace `_committed_tf_files`, `_find_file_for`, `_committed_addresses`,
  `_emitted_identities`, lexical variable/import discovery, and lexical
  dangling-reference discovery with `ModuleIndex` queries.

- [ ] Make `reconcile_renderer.py` translate every `SourceAnchor` to one indexed
  `BytePatch`. Verify the snapshot source hash before patch construction and let
  the phase-3 transaction perform the final pre-commit identity/hash recheck.

- [ ] Remove all production callers of handwritten scanner helpers. Retain only
  the documented compatibility API in `hcl_surgeon.py`.

- [ ] Verify:

  ```bash
  python3 -m pytest tests/test_hcl_index.py tests/test_hcl_patches.py \
    tests/test_hcl_surgeon.py tests/test_module_index.py \
    tests/test_reconcile.py tests/test_reconcile_renderer.py \
    tests/test_pipeline.py tests/test_properties.py -q
  python3 -m ruff check .
  python3 -m mypy src
  git diff --check
  ```

- [ ] Commit:

  ```bash
  git add src/ubitofu/reconcile_snapshot.py \
    src/ubitofu/reconcile_renderer.py src/ubitofu/reconcile_planner.py \
    src/ubitofu/pipeline.py tests/test_reconcile.py \
    tests/test_reconcile_renderer.py tests/test_pipeline.py
  git commit -m "reconcile: anchor source discovery and edits structurally"
  ```

---

### Task 6: Mutation-test and release-verify structural editing

**Files:**

- Modify: `pyproject.toml`
- Modify: `ci/mutation_gate.py`
- Modify: `.woodpecker/ci.yml`
- Modify: `tests/test_infra_deps.py`
- Modify: `docs/testing.md`
- Modify: `docs/hcl-parser-decision.md`

- [ ] Add `hcl_index.py`, `hcl_patches.py`, `module_index.py`, and the retained
  `hcl_surgeon.py` compatibility layer to mutation scope and CI path filters.

- [ ] Kill meaningful survivors involving error-node checks, byte boundaries,
  duplicate detection, precedence direction, expected bytes, overlap, and patch
  order. Annotate only proven equivalent native-wrapper mutations.

- [ ] Run:

  ```bash
  python3 -m pytest -m "not controller" --cov=ubitofu --cov-branch
  python3 -m pytest proofs/test_hcl_parser_proof.py -q
  python3 -m ruff check .
  python3 -m mypy src
  python3 ci/mutation_gate.py check
  python3 -m build
  git diff --check
  ```

- [ ] Run package-install smoke tests on supported macOS/Linux architectures and
  the selected live-controller reconcile scenarios. Inspect output bytes for all
  current fixtures and compare intentional differences only.

- [ ] Update `docs/hcl-parser-decision.md` to mark production rollout complete,
  name the runtime modules, and retain the original proof evidence.

- [ ] Request an adversarial review focused on parser recovery, Unicode/CRLF
  byte offsets, heredoc interpolation, duplicate anchors, file precedence,
  JSON attention policy, patch overlap, packaging, and transaction integration.

- [ ] Commit:

  ```bash
  git add pyproject.toml ci/mutation_gate.py .woodpecker/ci.yml \
    tests/test_infra_deps.py docs/testing.md docs/hcl-parser-decision.md
  git commit -m "ci: gate structural HCL source safety"
  ```

## Completion Gate

Phase 4 is complete only when the production index matches all 53 proof spans,
rejects all proof-invalid inputs and every recovery node, current reconcile
fixtures preserve all unselected bytes, JSON edit-required drift is attention,
OpenTofu precedence is tested, package installation passes on supported targets,
and structural mutations have no unexplained survivors.
