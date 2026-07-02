# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 James Braid
from unifi_tofu_import.enumerator import ImportTarget
from unifi_tofu_import.import_emitter import (
    assign_slugs,
    emit_import_blocks,
    slugify,
)


def test_slugify_normalizes() -> None:
    assert slugify("Work James") == "work_james"
    assert slugify("Internet 1") == "internet_1"
    assert slugify("iot-2.4") == "iot_2_4"


def test_dedup_within_resource_type() -> None:
    targets = [
        ImportTarget("unifi_network", "examplenet", "id1"),
        ImportTarget("unifi_network", "examplenet", "id2"),
    ]
    slugs = [s for _, s in assign_slugs(targets)]
    assert slugs == ["examplenet", "examplenet_2"]


def test_same_slug_different_type_not_deduped() -> None:
    targets = [
        ImportTarget("unifi_network", "lan", "id1"),
        ImportTarget("unifi_wan", "lan", "id2"),
    ]
    slugs = [s for _, s in assign_slugs(targets)]
    assert slugs == ["lan", "lan"]


def test_emit_import_blocks_shape() -> None:
    out = emit_import_blocks([ImportTarget("unifi_network", "examplenet", "abc123")])
    assert 'to = unifi_network.examplenet' in out
    assert 'id = "abc123"' in out
    assert out.strip().startswith("import {")
