# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 James Braid
import re

from .enumerator import ImportTarget


def slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", name.strip().lower()).strip("_")
    if slug and slug[0].isdigit():
        slug = f"n_{slug}"
    return slug or "unnamed"


def assign_slugs(
    targets: list[ImportTarget], reserved: set[str] | None = None
) -> list[tuple[ImportTarget, str]]:
    reserved = reserved or set()
    seen: dict[tuple[str, str], int] = {}
    used: set[str] = set()  # all slugs assigned so far in this batch
    out: list[tuple[ImportTarget, str]] = []
    for t in targets:
        base = slugify(t.name_hint)
        key = (t.resource_type, base)
        seen[key] = seen.get(key, 0) + 1
        n = seen[key]
        slug = base if n == 1 else f"{base}_{n}"
        # Guard against both reserved slugs and intra-batch collisions.  The
        # latter arise when a suffix-decorated slug (e.g. "a_2") coincides with
        # a slug generated from a distinct name that slugifies to the same base
        # (e.g. name "a_2" → base "a_2" → slug "a_2").
        qualified = f"{t.resource_type}.{slug}"
        while qualified in reserved or qualified in used:
            n += 1
            slug = f"{base}_{n}"
            qualified = f"{t.resource_type}.{slug}"
        seen[key] = n  # remember the highest suffix consumed so the next same-base target skips it
        used.add(qualified)
        out.append((t, slug))
    return out


def emit_import_blocks(targets: list[ImportTarget]) -> str:
    blocks = []
    for t, slug in assign_slugs(targets):
        blocks.append(
            f'import {{\n  to = {t.resource_type}.{slug}\n  id = "{t.import_id}"\n}}')
    return "\n\n".join(blocks) + "\n"
