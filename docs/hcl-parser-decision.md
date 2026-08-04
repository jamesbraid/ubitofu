# HCL parser span decision

## Decision

Use tree-sitter-hcl 1.2.0 with tree-sitter 0.26.0 for the production structural
index. Version 0.10 routes active-source discovery and anchored edits through
that index, then commits the complete candidate set through the recoverable
transaction layer.

tree-sitter-hcl matched the complete ordered corpus: 53 literal block,
attribute, and qualified-reference spans across 8 valid cases. It rejected all
3 invalid cases. python-hcl2 matched structural spans and qualified references
in ordinary and quoted-template expressions, but missed `${var.region}` inside
a heredoc because its raw Lark tree represents the full heredoc as one opaque
token. That fails the reference-discovery contract, so python-hcl2's zero
incremental footprint does not outweigh the correctness gap.

The Go fallback was not attempted because tree-sitter-hcl passed.

## Contract proved

The proof corpus contains native `.tf` and `.tofu` inputs with LF, CRLF,
mixed line endings, Unicode before and inside spans, a file without a final
newline, comments, nested blocks, duplicate-looking text, heredoc decoys,
interpolation, variables, imports, and qualified expression references. It also
includes every current reconcile fixture. `after.tf`, at 502 bytes, is the
largest HCL module currently available in the repository.

The oracle compares the complete ordered `Span` list. It rejects missing,
unexpected, reordered, or duplicated spans. The reconcile fixtures now specify
all emitted spans: 9 for `before.tf`, 9 for `after.tf`, and 3 for
`committed.tf`.

Reference evidence covers qualified traversals with at least one attribute
step. The corpus checks `var.region`, `var.name`, and `unifi_network.edge` in
quoted interpolation, heredoc interpolation, object values, ordinary
expressions, and import targets. Each expected reference has a literal identity
and original-byte range. Bare identifiers such as type constraints, object keys,
and the special `ignore_changes` value are outside this proof's reference
contract.

The BOM policy is explicit: reject a leading UTF-8 BOM. Both adapters also
reject invalid UTF-8. tree-sitter parses original bytes directly, then the
adapter walks the complete tree and rejects every node with `is_error`,
`is_missing`, type `ERROR`, or type `MISSING`. The malformed assignment produced
`ERROR`. The unclosed block produced a missing `}`.

python-hcl2 receives an LF-normalized parser view derived from raw bytes. A
total map with `len(parser_text) + 1` entries maps every normalized character
boundary back to the original byte stream. That coordinate adapter passes, but
the opaque heredoc token prevents complete structural reference traversal.

No tree-sitter grammar miss appeared in the valid checked corpus. This is
bounded evidence, not a claim that tree-sitter-hcl accepts every HCL program.

## Reproduce the decision

`tools/hcl_parser_proof.py` pins the candidate and parser-stack versions with
inline script metadata. Run the comparison from the repository root:

```text
UV_CACHE_DIR=/tmp/ubitofu-hcl-proof-cache \
  uv run --script tools/hcl_parser_proof.py
```

The command prints both corpus results as JSON. It exits 0 only when
python-hcl2 retains the recorded reference gap and tree-sitter-hcl passes the
complete corpus. The isolated script environment does not change
`pyproject.toml` or install tree-sitter into ubitofu's runtime environment.
The focused regression suite lives at `proofs/test_hcl_parser_proof.py`, outside
the default pytest collection path.

## Versions and footprint

The proof script pins:

| Distribution | Version | Measured installed bytes |
| --- | ---: | ---: |
| python-hcl2 | 8.1.2 | 349,309 |
| lark | 1.3.1 | 358,152 |
| regex | 2026.7.19 | 1,250,826 |
| tree-sitter | 0.26.0 | 366,733 |
| tree-sitter-hcl | 1.2.0 | 151,546 |

The python-hcl2 stack totals 1,958,287 measured bytes and is already present for
ubitofu's writer. The selected tree-sitter stack totals 518,279 incremental
bytes across two distributions with native extensions. Byte totals sum
installed distribution files reported by `importlib.metadata`. They are not
wheel download sizes.

## Latency evidence

The recorded macOS 15.7.5 arm64 benchmark ran under Python 3.14.6. It parsed raw
`tests/fixtures/reconcile/after.tf` bytes after 100 warm-up parses, then measured
2,000 calls per adapter with garbage collection disabled during sampling.

| Candidate | Mean | p50 | p95 | Minimum |
| --- | ---: | ---: | ---: | ---: |
| python-hcl2 | 791.991 us | 693.479 us | 1,056.542 us | 645.042 us |
| tree-sitter-hcl | 123.921 us | 116.416 us | 167.000 us | 108.042 us |

This benchmark ranks disposable adapters on one small real module. It does not
establish a general parser throughput result.

## Production status

The 0.10 cutover completed the rollout gate. `module_index.py` owns effective
source precedence and structural discovery. `hcl_index.py` owns byte spans, and
`hcl_patches.py` applies verified replacements without rewriting unselected
bytes. The old handwritten scanner and writer were deleted. python-hcl2 remains
only in the serializer for ubitofu-owned HCL.
