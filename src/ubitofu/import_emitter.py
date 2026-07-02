# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 James Braid
import re

from .enumerator import ImportTarget


def slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", name.strip().lower()).strip("_")
    if slug and slug[0].isdigit():
        slug = f"n_{slug}"
    return slug or "unnamed"


def assign_slugs(targets: list[ImportTarget]) -> list[tuple[ImportTarget, str]]:
    seen: dict[tuple[str, str], int] = {}
    out: list[tuple[ImportTarget, str]] = []
    for t in targets:
        base = slugify(t.name_hint)
        key = (t.resource_type, base)
        seen[key] = seen.get(key, 0) + 1
        slug = base if seen[key] == 1 else f"{base}_{seen[key]}"
        out.append((t, slug))
    return out


def emit_import_blocks(targets: list[ImportTarget]) -> str:
    blocks = []
    for t, slug in assign_slugs(targets):
        blocks.append(
            f'import {{\n  to = {t.resource_type}.{slug}\n  id = "{t.import_id}"\n}}')
    return "\n\n".join(blocks) + "\n"
