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


def test_slugify_strips_edge_underscores() -> None:
    # Leading/trailing non-alnum collapse to underscores that must be stripped.
    assert slugify("!foo!") == "foo"
    assert slugify("  hi there  ") == "hi_there"


def test_slugify_digit_prefix_gets_n_prefix() -> None:
    assert slugify("123") == "n_123"
    assert slugify("42 devices") == "n_42_devices"


def test_slugify_empty_becomes_unnamed() -> None:
    # A name with no alnum content slugifies to empty, then the fallback applies.
    assert slugify("!!!") == "unnamed"
    assert slugify("") == "unnamed"


def test_emit_import_blocks_exact_output() -> None:
    out = emit_import_blocks([
        ImportTarget("unifi_network", "a", "1"),
        ImportTarget("unifi_network", "b", "2"),
    ])
    expected = (
        'import {\n  to = unifi_network.a\n  id = "1"\n}\n\n'
        'import {\n  to = unifi_network.b\n  id = "2"\n}\n'
    )
    assert out == expected


def test_assign_slugs_used_collision_forces_distinct() -> None:
    # Third target's base ("lan_2") collides with the suffix slug assigned to the
    # second target; the `used` guard must bump it rather than emit a duplicate.
    targets = [
        ImportTarget("unifi_network", "lan", "1"),
        ImportTarget("unifi_network", "lan", "2"),
        ImportTarget("unifi_network", "lan_2", "3"),
    ]
    slugs = [s for _, s in assign_slugs(targets)]
    assert slugs == ["lan", "lan_2", "lan_2_2"]
    assert len(set(slugs)) == 3


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
