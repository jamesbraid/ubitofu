# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 James Braid
"""Tests for effective OpenTofu source selection and read-only JSON indexing."""

from __future__ import annotations

from pathlib import PurePosixPath

import pytest

from ubitofu.module_index import index_effective_module, reindex_module
from ubitofu.values import FrozenObject


def _index(tmp_path, *candidates: tuple[str, bytes | None]):
    return index_effective_module(
        workdir=tmp_path,
        candidates=tuple((PurePosixPath(path), source) for path, source in candidates),
    )


def test_index_retains_detached_source_bytes_after_the_workdir_changes(tmp_path) -> None:
    """Catches a snapshot index rereading mutable source after collection."""
    source = b'resource "widget" "captured" {}\n'
    (tmp_path / "main.tf").write_bytes(source)

    index = _index(tmp_path)
    (tmp_path / "main.tf").write_bytes(b'resource "widget" "changed" {}\n')

    assert index.sources[0].source == source
    assert [resource.address for resource in reindex_module(index, ()).resources] == [
        "widget.captured"
    ]


def test_pure_reindex_applies_shadow_and_deletion_over_retained_bytes(tmp_path) -> None:
    """Catches reindexing overlays by rereading the filesystem or losing shadow state."""
    (tmp_path / "main.tf").write_bytes(b'resource "widget" "terraform" {}\n')
    (tmp_path / "main.tofu").write_bytes(b'resource "widget" "tofu" {}\n')
    index = _index(tmp_path)
    (tmp_path / "main.tf").unlink()
    (tmp_path / "main.tofu").write_bytes(b'resource "widget" "mutated" {}\n')

    reexposed = reindex_module(index, ((PurePosixPath("main.tofu"), None),))

    assert [resource.address for resource in reexposed.resources] == ["widget.terraform"]
    assert [(source.relative_path, source.active) for source in reexposed.sources] == [
        (PurePosixPath("main.tf"), True)
    ]


def test_tofu_shadows_the_same_stem_tf_but_keeps_it_as_inactive_metadata(tmp_path) -> None:
    """Catches indexing Terraform compatibility files as editable OpenTofu source."""
    (tmp_path / "network.tf").write_bytes(b'resource "widget" "terraform" {}\n')
    (tmp_path / "network.tofu").write_bytes(b'resource "widget" "tofu" {}\n')

    index = _index(tmp_path)

    assert [(source.relative_path, source.active) for source in index.sources] == [
        (PurePosixPath("network.tf"), False),
        (PurePosixPath("network.tofu"), True),
    ]
    assert [
        (resource.address, resource.source_path, resource.editable) for resource in index.resources
    ] == [("widget.tofu", PurePosixPath("network.tofu"), True)]


def test_tofu_json_shadows_same_stem_tf_json_but_native_and_json_both_participate(tmp_path) -> None:
    """Catches applying source precedence across native and JSON syntaxes."""
    (tmp_path / "native.tf").write_bytes(b'resource "widget" "native" {}\n')
    (tmp_path / "generated.tf.json").write_bytes(b'{"resource":{"widget":{"tf_json":{}}}}')
    (tmp_path / "generated.tofu.json").write_bytes(b'{"resource":{"widget":{"tofu_json":{}}}}')

    index = _index(tmp_path)

    assert [(source.relative_path, source.active) for source in index.sources] == [
        (PurePosixPath("generated.tf.json"), False),
        (PurePosixPath("generated.tofu.json"), True),
        (PurePosixPath("native.tf"), True),
    ]
    assert [(resource.address, resource.editable) for resource in index.resources] == [
        ("widget.native", True),
        ("widget.tofu_json", False),
    ]
    json_source = next(
        source for source in index.sources if source.relative_path.name == "generated.tofu.json"
    )
    assert json_source.json_value == FrozenObject(
        (
            (
                "resource",
                FrozenObject((("widget", FrozenObject((("tofu_json", FrozenObject(())),))),)),
            ),
        )
    )


def test_override_resource_replaces_the_effective_edit_target_after_normal_sources(
    tmp_path,
) -> None:
    """Catches treating an OpenTofu override as an ordinary duplicate address."""
    (tmp_path / "main.tf").write_bytes(b'resource "widget" "edge" { name = "base" }\n')
    (tmp_path / "main_override.tofu").write_bytes(
        b'resource "widget" "edge" { name = "override" }\n'
    )

    index = _index(tmp_path)

    assert [(resource.address, resource.source_path) for resource in index.resources] == [
        ("widget.edge", PurePosixPath("main_override.tofu"))
    ]


def test_candidate_source_shadows_disk_and_deletion_exposes_the_shadowed_source(tmp_path) -> None:
    """Catches a staged deletion leaving a disk source permanently suppressed."""
    (tmp_path / "main.tf").write_bytes(b'resource "widget" "terraform" {}\n')
    (tmp_path / "main.tofu").write_bytes(b'resource "widget" "tofu" {}\n')

    shadowed = _index(tmp_path, ("main.tofu", b'resource "widget" "candidate" {}\n'))
    exposed = _index(tmp_path, ("main.tofu", None))

    assert [resource.address for resource in shadowed.resources] == ["widget.candidate"]
    assert [resource.address for resource in exposed.resources] == ["widget.terraform"]


def test_duplicate_active_resource_addresses_fail_closed(tmp_path) -> None:
    """Catches silently choosing a config object OpenTofu would reject as duplicate."""
    (tmp_path / "a.tf").write_bytes(b'resource "widget" "edge" {}\n')
    (tmp_path / "b.tf").write_bytes(b'resource "widget" "edge" {}\n')

    with pytest.raises(ValueError, match="duplicate resource"):
        _index(tmp_path)


@pytest.mark.parametrize(
    "source",
    [
        b'{"resource":',
        b'{"resource": {}, "resource": {}}',
        b'{"resource": NaN}',
    ],
)
def test_invalid_or_duplicate_key_json_fails_closed(tmp_path, source: bytes) -> None:
    """Catches mutable, partial, or duplicate-key generated source being accepted."""
    (tmp_path / "generated.tf.json").write_bytes(source)

    with pytest.raises(ValueError, match="JSON"):
        _index(tmp_path)


def test_indexes_native_imports_variables_and_qualified_references(tmp_path) -> None:
    """Catches later callers needing a second handwritten HCL discovery path."""
    (tmp_path / "main.tf").write_bytes(
        b'variable "token" {}\n'
        b'resource "widget" "edge" { value = widget.other.name }\n'
        b'import { to = widget.edge id = "edge-id" }\n'
    )

    index = _index(tmp_path)

    assert [(item.name, item.source_path) for item in index.variables] == [
        ("token", PurePosixPath("main.tf"))
    ]
    assert [(item.address, item.import_id) for item in index.imports] == [
        ("widget.edge", "edge-id")
    ]
    assert [(item.target_address, item.source_path) for item in index.references] == [
        ("widget.other.name", PurePosixPath("main.tf")),
        ("widget.edge", PurePosixPath("main.tf")),
    ]


def test_repeated_native_import_blocks_keep_each_target_paired_with_its_own_id(tmp_path) -> None:
    """Catches unlabeled import blocks sharing the first block's attributes."""
    (tmp_path / "imports.tf").write_bytes(
        b'import {\n  to = widget.first\n  id = "first-id"\n}\n'
        b'import {\n  to = widget.second\n  id = "second-id"\n}\n'
    )

    index = _index(tmp_path)

    assert [(item.address, item.import_id) for item in index.imports] == [
        ("widget.first", "first-id"),
        ("widget.second", "second-id"),
    ]


def test_json_imports_and_references_follow_active_source_precedence_and_are_read_only(
    tmp_path,
) -> None:
    """Catches ignoring active JSON imports and expression-only references."""
    (tmp_path / "generated.tf.json").write_bytes(
        b'{"import":[{"to":"widget.shadowed","id":"old-id"}]}'
    )
    (tmp_path / "generated.tofu.json").write_bytes(
        b'{"resource":{"widget":{"target":{},"consumer":'
        b'{"value":"${widget.target.name}"}}},'
        b'"import":[{"to":"widget.target","id":"target-id"}]}'
    )

    index = _index(tmp_path)

    assert [(item.address, item.import_id, item.source_path) for item in index.imports] == [
        ("widget.target", "target-id", PurePosixPath("generated.tofu.json"))
    ]
    assert [(item.target_address, item.expression) for item in index.references] == [
        ("widget.target", None),
        ("widget.target.name", None),
    ]
    assert all(resource.editable is False for resource in index.resources)


@pytest.mark.parametrize(
    "source",
    [
        b"[]",
        b'{"resource":[]}',
        b'{"resource":{"widget":[]}}',
        b'{"resource":{"widget":{"edge":[]}}}',
        b'{"variable":{"name":[]}}',
        b'{"import":{}}',
        b'{"import":[{"to":"widget.edge"}]}',
        b'{"import":[{"to":"${widget.edge}","id":"edge-id"}]}',
        b'{"import":[{"to":"widget.edge","id":"edge-id","identity":{}}]}',
        b'{"resource":{"widget":{"edge":{"value":"prefix ${widget.other + var.extra}"}}}}',
    ],
)
def test_invalid_json_module_shapes_and_ambiguous_references_fail_closed(
    tmp_path,
    source: bytes,
) -> None:
    """Catches indexing malformed JSON source as an active empty module."""
    (tmp_path / "generated.tf.json").write_bytes(source)

    with pytest.raises(ValueError, match="JSON"):
        _index(tmp_path)


def test_json_references_cover_non_resource_configuration_and_ignore_inactive_source(
    tmp_path,
) -> None:
    """Catches reference discovery limited to resource bodies or shadowed JSON."""
    (tmp_path / "references.tf.json").write_bytes(
        b'{"output":{"shadowed":{"value":"${widget.shadowed}"}}}'
    )
    (tmp_path / "references.tofu.json").write_bytes(
        b'{"variable":{"copy":{"default":"${var.upstream}"}},'
        b'"output":{"result":{"value":"prefix ${widget.target.name} suffix"}},'
        b'"locals":{"literal":"$${widget.literal}",'
        b'"alias":"${widget.target}"}}'
    )

    index = _index(tmp_path)

    assert {(item.target_address, item.expression) for item in index.references} == {
        ("var.upstream", None),
        ("widget.target", None),
        ("widget.target.name", None),
    }


def test_json_templates_distinguish_escaped_literal_complete_and_mixed_references(tmp_path) -> None:
    """Catches rejecting HCL's escaped interpolation marker or losing real traversals."""
    (tmp_path / "templates.tf.json").write_bytes(
        b'{"resource":{"widget":{"consumer":{'
        b'"literal":"$${widget.literal}",'
        b'"complete":"${widget.complete}",'
        b'"mixed":"before ${widget.mixed} after"'
        b"}}}}"
    )

    index = _index(tmp_path)

    assert {(item.target_address, item.expression) for item in index.references} == {
        ("widget.complete", None),
        ("widget.mixed", None),
    }


@pytest.mark.parametrize(
    "comment",
    ["${widget.edge}", "${var.region + 1}"],
)
def test_top_level_json_comments_do_not_contribute_or_validate_references(
    tmp_path, comment: str
) -> None:
    """Catches scanning top-level JSON comments as OpenTofu configuration."""
    (tmp_path / "comments.tf.json").write_bytes(
        b'{"//":"' + comment.encode() + b'","locals":{"real":"${widget.real}"}}'
    )

    index = _index(tmp_path)

    assert {(item.target_address, item.expression) for item in index.references} == {
        ("widget.real", None),
    }
