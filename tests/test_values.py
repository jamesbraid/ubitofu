# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 James Braid
import math

import pytest

from ubitofu.values import FrozenObject, freeze_value


def test_freeze_value_copies_nested_values_and_canonicalizes_object_keys():
    source = {"z": ["one", {"b": True, "a": None}], "a": (1, 2.5)}

    frozen = freeze_value(source)
    source["z"][1]["a"] = "changed"

    assert frozen == FrozenObject(
        (("a", (1, 2.5)), ("z", ("one", FrozenObject((("a", None), ("b", True))))))
    )


@pytest.mark.parametrize(
    "value", [{1: "not-a-string-key"}, {"key": {1, 2}}, math.nan, math.inf, -math.inf]
)
def test_freeze_value_rejects_values_without_a_stable_immutable_representation(value):
    with pytest.raises(ValueError):
        freeze_value(value)
