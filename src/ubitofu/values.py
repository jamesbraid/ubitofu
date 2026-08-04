# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 James Braid
"""Immutable, deterministic values retained from external documents."""

import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TypeAlias

Scalar: TypeAlias = None | bool | int | float | str


@dataclass(frozen=True)
class FrozenObject:
    items: tuple[tuple[str, "FrozenValue"], ...]


FrozenValue: TypeAlias = Scalar | tuple["FrozenValue", ...] | FrozenObject


def freeze_value(value: object) -> FrozenValue:
    """Copy supported JSON-shaped input into one immutable representation."""
    if value is None or isinstance(value, bool | str | int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("non-finite float is not a supported external value")
        return value
    if isinstance(value, Mapping):
        if not all(isinstance(key, str) for key in value):
            raise ValueError("external object keys must be strings")
        return FrozenObject(tuple((key, freeze_value(value[key])) for key in sorted(value)))
    if isinstance(value, list | tuple):
        return tuple(freeze_value(item) for item in value)
    raise ValueError(f"unsupported external value type: {type(value).__name__}")
