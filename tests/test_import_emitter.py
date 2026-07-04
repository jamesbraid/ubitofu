# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 James Braid
from ubitofu.enumerator import ImportTarget
from ubitofu.import_emitter import (
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


def test_assign_slugs_skips_reserved_addresses():
    targets = [ImportTarget("unifi_device", "U7 Pro Wall", "58:d6:1f:00:00:0b")]
    reserved = {"unifi_device.u7_pro_wall"}  # already owned by a managed resource
    out = assign_slugs(targets, reserved=reserved)
    slug = out[0][1]
    assert slug != "u7_pro_wall"
    assert slug == "u7_pro_wall_2"
    assert f"unifi_device.{slug}" not in reserved


def test_assign_slugs_reserved_default_is_backcompat():
    targets = [ImportTarget("unifi_device", "U7 Pro Wall", "58:d6:1f:00:00:0b")]
    assert assign_slugs(targets)[0][1] == "u7_pro_wall"  # unchanged when no reserved set


def test_assign_slugs_reserved_and_intra_batch_collision_both_avoided():
    # Two new same-name targets AND one reserved -> _2 is taken by reserved, so _2 and _3.
    targets = [
        ImportTarget("unifi_device", "U7 Pro Wall", "58:d6:1f:00:00:0b"),
        ImportTarget("unifi_device", "U7 Pro Wall", "58:d6:1f:00:00:0c"),
    ]
    reserved = {"unifi_device.u7_pro_wall"}
    slugs = [s for _, s in assign_slugs(targets, reserved=reserved)]
    assert slugs == ["u7_pro_wall_2", "u7_pro_wall_3"]
    assert len(set(slugs)) == 2
