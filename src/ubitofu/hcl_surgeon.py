# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 James Braid
"""Comment-preserving, in-place surgery on committed OpenTofu HCL.

The generate path renders HCL wholesale (python-hcl2) and drops all comments
and layout. Reconcile must do the opposite: nudge a single scalar value in an
operator-maintained file while leaving indentation, comments, blank lines and
every other byte untouched. That rules out load-then-dumps; this module works
at the text level with a small brace/string/comment-aware scanner.

Anchoring is threefold, so the wrong occurrence is never edited:
  1. the resource block is located by ``resource "TYPE" "SLUG"`` (unique slug);
  2. only TOP-LEVEL assignments (brace-depth 0 inside the block) are matched,
     so a nested ``enabled = true`` never masquerades as the top-level one;
  3. the value is edited only when the committed literal equals the serialized
     OLD value (parse-for-comparison, never parse-to-rewrite).

Known limitations (documented, out of scope for UniFi scalar values): HCL
heredocs (``<<EOT``) and ``${…}`` interpolations that embed unbalanced quotes
are not modelled by the scanner. UniFi controller scalars (names, IPs, VLAN
ids, booleans) never take those shapes.
"""

import re

from .hcl_writer import _q


def _skip_string(text: str, i: int) -> int:
    """Given text[i] == '"', return the index just past the closing quote."""
    i += 1
    n = len(text)
    while i < n:
        c = text[i]
        if c == "\\":
            i += 2
            continue
        if c == '"':
            return i + 1
        i += 1
    return i


def _skip_line_comment(text: str, i: int) -> int:
    """Return the index just past the end-of-line terminating a # or // comment."""
    nl = text.find("\n", i)
    return len(text) if nl == -1 else nl + 1


def _skip_block_comment(text: str, i: int) -> int:
    """Return the index just past the ``*/`` closing a ``/* … */`` comment."""
    end = text.find("*/", i + 2)
    return len(text) if end == -1 else end + 2


def _match_brace(text: str, i: int) -> int | None:
    """text[i] == '{'; return the index of the matching '}', or None.

    Braces inside strings and comments do not count toward nesting depth.
    """
    depth = 0
    n = len(text)
    while i < n:
        c = text[i]
        if c == '"':
            i = _skip_string(text, i)
            continue
        if c == "#" or text[i:i + 2] == "//":
            i = _skip_line_comment(text, i)
            continue
        if text[i:i + 2] == "/*":
            i = _skip_block_comment(text, i)
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return None


def _locate(text: str, resource_type: str, slug: str) -> tuple[int, int, int] | None:
    """Return (block_start, open_brace_index, close_brace_index) or None."""
    header = re.compile(
        r'resource\s+"' + re.escape(resource_type)
        + r'"\s+"' + re.escape(slug) + r'"\s*\{'
    )
    m = header.search(text)
    if m is None:
        return None
    open_brace = m.end() - 1
    close = _match_brace(text, open_brace)
    if close is None:
        return None
    return m.start(), open_brace, close


def find_resource_block_span(
    text: str, resource_type: str, slug: str
) -> tuple[int, int] | None:
    """Locate ``resource "TYPE" "SLUG" { … }`` and return its (start, end) span.

    ``end`` is one past the matching closing brace, so ``text[start:end]`` is the
    whole block. Returns None when the block is absent.
    """
    loc = _locate(text, resource_type, slug)
    if loc is None:
        return None
    start, _open, close = loc
    return start, close + 1


def _strip_inline_comment(s: str) -> str:
    """Drop a trailing # or // comment that is not inside a string literal."""
    i, n = 0, len(s)
    while i < n:
        c = s[i]
        if c == '"':
            i = _skip_string(s, i)
            continue
        if c == "#" or s[i:i + 2] == "//":
            return s[:i]
        i += 1
    return s


def _top_level_assignments(inner: str) -> dict[str, tuple[int, int]]:
    """Map each TOP-LEVEL ``attr = value`` to (value_start, value_end) offsets.

    Offsets are relative to *inner* (the block body between its braces). Only
    assignments at brace-depth 0 are returned; the value span excludes the
    surrounding whitespace and any trailing inline comment. Scalar values live
    on one line, which is all reconcile ever edits.
    """
    out: dict[str, tuple[int, int]] = {}
    i, n, depth = 0, len(inner), 0
    while i < n:
        c = inner[i]
        if c == '"':
            i = _skip_string(inner, i)
            continue
        if c == "#" or inner[i:i + 2] == "//":
            i = _skip_line_comment(inner, i)
            continue
        if inner[i:i + 2] == "/*":
            i = _skip_block_comment(inner, i)
            continue
        if c in "{[(":
            depth += 1
            i += 1
            continue
        if c in "}])":
            depth -= 1
            i += 1
            continue
        if depth == 0 and (c.isalpha() or c == "_"):
            j = i
            while j < n and (inner[j].isalnum() or inner[j] in "_-"):  # noqa: E501  # pragma: no mutate — equivalent: the only chars a `"_-"`→wider-set mutation adds are alphanumeric (e.g. 'X'), which `inner[j].isalnum()` already matches, so the disjunction is unchanged for every input; the killable loop-bound mutant on this line is covered by test_update_with_trailing_bare_identifier
                j += 1
            name = inner[i:j]
            k = j
            while k < n and inner[k] in " \t":
                k += 1
            # A single '=' introduces an assignment; '==' etc. cannot appear at
            # the top level of a resource body, but guard anyway.
            if k < n and inner[k] == "=" and inner[k:k + 2] != "==":
                v = k + 1
                while v < n and inner[v] in " \t":
                    v += 1
                eol = inner.find("\n", v)
                if eol == -1:
                    eol = n  # noqa: E501  # pragma: no mutate — equivalent: eol now only bounds the inner[v:eol] slice (the scan resumes at v, not eol), and an n→None mutation slices to the same end of string
                val_text = _strip_inline_comment(inner[v:eol]).rstrip()
                out.setdefault(name, (v, v + len(val_text)))
                # Resume scanning AT the value, not past it: a multiline
                # collection value (`x = [` … `]`) opens brackets on this line
                # that the scanner must count, or depth goes negative at the
                # closer and every later top-level attr becomes invisible.
                i = v
                continue
            i = j
            continue
        i += 1
    return out


def _serialize(value: object) -> str:
    """Serialize a scalar to its canonical HCL literal (generate-parity)."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return str(int(value)) if value.is_integer() else repr(value)
    if isinstance(value, str):
        return _q(value)
    raise TypeError(f"not a scalar HCL value: {value!r}")


def update_scalar(
    text: str,
    resource_type: str,
    slug: str,
    attr: str,
    old_value: object,
    new_value: object,
) -> str:
    """Replace one top-level scalar value in place, preserving everything else.

    The edit happens only when the committed literal for ``attr`` equals the
    serialized ``old_value`` — the anchor that stops us touching a hand-changed
    line. Raises LookupError when the block or attr is absent, ValueError on an
    anchor mismatch. When ``old_value == new_value`` the return is byte-identical
    (idempotent).
    """
    loc = _locate(text, resource_type, slug)
    if loc is None:
        raise LookupError(f'resource "{resource_type}" "{slug}" not found')
    _start, open_brace, close = loc
    inner = text[open_brace + 1:close]
    assignments = _top_level_assignments(inner)
    if attr not in assignments:
        raise LookupError(
            f"{resource_type}.{slug} has no top-level scalar attr {attr!r}")
    val_start, val_end = assignments[attr]
    committed_literal = inner[val_start:val_end]
    expected = _serialize(old_value)
    if committed_literal != expected:
        raise ValueError(
            f"anchor mismatch for {resource_type}.{slug}.{attr}: committed "
            f"{committed_literal!r} != serialized old {expected!r}")
    abs_start = open_brace + 1 + val_start
    abs_end = open_brace + 1 + val_end
    return text[:abs_start] + _serialize(new_value) + text[abs_end:]
