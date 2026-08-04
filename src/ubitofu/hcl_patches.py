# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 James Braid
"""Validated transactional edits over original source bytes."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from .hcl_index import ByteSpan


@dataclass(frozen=True)
class BytePatch:
    """One anchored replacement against unmodified source bytes."""

    span: ByteSpan
    expected: bytes
    replacement: bytes
    reason: str


def apply_patches(source: bytes, patches: Iterable[BytePatch]) -> bytes:
    """Apply non-overlapping anchored patches without changing other bytes."""
    ordered = tuple(sorted(patches, key=lambda patch: (patch.span.start, patch.span.end)))
    _validate(source, ordered)
    output = source
    for patch in reversed(ordered):
        output = output[: patch.span.start] + patch.replacement + output[patch.span.end :]
    return output


def _validate(source: bytes, patches: tuple[BytePatch, ...]) -> None:
    previous: BytePatch | None = None
    for patch in patches:
        start = patch.span.start
        end = patch.span.end
        if not 0 <= start <= end <= len(source):
            raise ValueError("patch bounds are outside source")
        if source[start:end] != patch.expected:
            raise ValueError("patch expected bytes do not match source")
        if previous is not None:
            if previous.span == patch.span:
                raise ValueError("duplicate patch span")
            if start < previous.span.end or start == previous.span.start:
                raise ValueError("patch spans overlap")
        previous = patch
