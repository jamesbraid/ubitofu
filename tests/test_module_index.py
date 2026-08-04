# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 James Braid
"""Tests for effective OpenTofu source selection and read-only JSON indexing."""

from __future__ import annotations

from pathlib import PurePosixPath

import pytest

from ubitofu.module_index import index_effective_module
from ubitofu.values import FrozenObject


def _index(tmp_path, *candidates: tuple[str, bytes | None]):
    return index_effective_module(
        workdir=tmp_path,
        candidates=tuple((PurePosixPath(path), source) for path, source in candidates),
    )


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
