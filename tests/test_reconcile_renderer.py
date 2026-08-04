# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 James Braid
"""Tests for immutable reconciliation candidate rendering."""

from __future__ import annotations

import hashlib
from dataclasses import replace
from pathlib import PurePosixPath

import pytest

from ubitofu.module_index import index_effective_module
from ubitofu.reconcile_model import (
    AppendImport,
    AppendResource,
    Disposition,
    FileIdentity,
    ReasonCode,
    ReconcilePlan,
    ReconcileSnapshot,
    ResourceDecision,
    SourceAnchor,
    UpdateScalar,
    parse_opentofu_address,
)
from ubitofu.values import freeze_value


def _snapshot(tmp_path, source: bytes, *, name: str = "main.tf") -> ReconcileSnapshot:
    return _snapshot_files(tmp_path, {name: source})


def _snapshot_files(tmp_path, files: dict[str, bytes]) -> ReconcileSnapshot:
    for name, source in files.items():
        (tmp_path / name).write_bytes(source)
    module = index_effective_module(workdir=tmp_path)
    identities = tuple(
        FileIdentity(
            PurePosixPath(name), 1, 2, 0o100644, 3, 4, len(source), 5,
            hashlib.sha256(source).hexdigest(),
        )
        for name, source in sorted(files.items())
    )
    return ReconcileSnapshot((), module, identities, "controller")


def _plan(*edits) -> ReconcilePlan:
    address = parse_opentofu_address("unifi_network.lan")
    return ReconcilePlan((
        ResourceDecision(address, Disposition.CAPTURE_LIVE, ReasonCode.LIVE_ONLY_CHANGE, edits, ()),
    ))


def test_render_scalar_update_preserves_comments_and_is_repeatable(tmp_path) -> None:
    """Catches rendering from evaluated values instead of the indexed expression token."""
    from ubitofu.reconcile_renderer import render_reconcile

    source = (
        b'# retain\nresource "unifi_network" "lan" {\n'
        b'  vlan = 10 # retain\n}\n'
    )
    snapshot = _snapshot(tmp_path, source)
    address = parse_opentofu_address("unifi_network.lan")
    plan = _plan(UpdateScalar(address, SourceAnchor(address, ("vlan",), b"10"), b"20"))

    first = render_reconcile(snapshot=snapshot, plan=plan)
    second = render_reconcile(snapshot=snapshot, plan=plan)

    assert first == second
    assert first.changed_paths == (PurePosixPath("main.tf"),)
    assert first.files[0].candidate == source.replace(b"vlan = 10", b"vlan = 20")
    assert first.files[0].candidate_sha256 == hashlib.sha256(first.files[0].candidate).hexdigest()


def test_render_returns_no_candidates_for_blocked_stale_or_mismatched_edits(tmp_path) -> None:
    """Catches exposing a partial candidate set after any precondition failure."""
    from ubitofu.reconcile_renderer import render_reconcile

    source = b'resource "unifi_network" "lan" { vlan = 10 }\n'
    snapshot = _snapshot(tmp_path, source)
    address = parse_opentofu_address("unifi_network.lan")
    mismatch = _plan(UpdateScalar(address, SourceAnchor(address, ("vlan",), b"11"), b"20"))

    preview = render_reconcile(snapshot=snapshot, plan=mismatch)

    assert preview.files == ()
    assert preview.changed_paths == ()
    assert preview.candidate_digests == ()


def test_render_rejects_a_stale_retained_source_identity(tmp_path) -> None:
    """Catches accepting a candidate whose retained bytes do not match snapshot identity."""
    from ubitofu.reconcile_renderer import render_reconcile

    source = b'resource "unifi_network" "lan" { vlan = 10 }\n'
    snapshot = _snapshot(tmp_path, source)
    stale = replace(snapshot.source_identities[0], sha256="0" * 64)
    snapshot = replace(snapshot, source_identities=(stale,))
    address = parse_opentofu_address("unifi_network.lan")
    plan = _plan(UpdateScalar(address, SourceAnchor(address, ("vlan",), b"10"), b"20"))

    assert render_reconcile(snapshot=snapshot, plan=plan).files == ()


def test_renderer_uses_retained_bytes_after_original_source_changes(tmp_path) -> None:
    """Catches a renderer reading a mutable destination rather than its snapshot."""
    from ubitofu.reconcile_renderer import render_reconcile

    source = b'resource "unifi_network" "lan" { vlan = 10 }\n'
    snapshot = _snapshot(tmp_path, source)
    (tmp_path / "main.tf").write_bytes(b'resource "unifi_network" "lan" { vlan = 99 }\n')
    address = parse_opentofu_address("unifi_network.lan")
    plan = _plan(UpdateScalar(address, SourceAnchor(address, ("vlan",), b"10"), b"20"))

    preview = render_reconcile(snapshot=snapshot, plan=plan)

    assert preview.files[0].candidate == b'resource "unifi_network" "lan" { vlan = 20 }\n'


def test_append_declares_only_renderer_owned_secret_binding(tmp_path) -> None:
    """Catches copying controller secret material or omitting its safe variable binding."""
    from ubitofu.reconcile_renderer import render_reconcile

    snapshot = _snapshot(tmp_path, b'terraform {}\n')
    address = parse_opentofu_address("unifi_wlan.guest")
    resource = freeze_value({"name": "guest"})
    assert resource.__class__.__name__ == "FrozenObject"
    plan = ReconcilePlan((
        ResourceDecision(
            address,
            Disposition.APPEND,
            ReasonCode.LIVE_RESOURCE_NEW,
            (AppendResource(address, resource), AppendImport(address, "synthetic-id")),
            (),
        ),
    ))

    preview = render_reconcile(snapshot=snapshot, plan=plan)
    candidates = {item.relative_path: item.candidate for item in preview.files}

    assert candidates[PurePosixPath("reconciled_new.tf")] == (
        b"# ubitofu: reconcile-preview v1\n\n"
        b'resource "unifi_wlan" "guest" {\n'
        b'  name       = "guest"\n'
        b"  passphrase = var.wlan_guest_psk\n"
        b"}\n"
        b"import {\n  to = unifi_wlan.guest\n  id = \"synthetic-id\"\n}\n"
    )
    assert candidates[PurePosixPath("unifi-variables.tf")] == (
        b"# ubitofu: reconcile-preview v1\n\n"
        b'variable "wlan_guest_psk" {\n  type      = string\n  sensitive = true\n}\n'
    )


def test_append_blocks_unknown_secret_shaped_values_without_rendering_them(tmp_path) -> None:
    """Catches a provider field that escapes projection sensitivity metadata."""
    from ubitofu.reconcile_renderer import render_reconcile

    snapshot = _snapshot(tmp_path, b'terraform {}\n')
    address = parse_opentofu_address("unifi_network.guest")
    resource = freeze_value({"name": "guest", "credential": "synthetic-raw-secret"})
    assert resource.__class__.__name__ == "FrozenObject"
    plan = ReconcilePlan((
        ResourceDecision(
            address,
            Disposition.APPEND,
            ReasonCode.LIVE_RESOURCE_NEW,
            (AppendResource(address, resource), AppendImport(address, "synthetic-id")),
            (),
        ),
    ))

    preview = render_reconcile(snapshot=snapshot, plan=plan)

    assert preview.files == ()
    assert all(
        item.candidate is None or b"synthetic-raw-secret" not in item.candidate
        for item in preview.files
    )


def test_append_blocks_a_nested_secret_named_like_a_safe_top_level_binding(tmp_path) -> None:
    """Catches treating a nested passphrase as the renderer-owned top-level binding."""
    from ubitofu.reconcile_renderer import render_reconcile

    snapshot = _snapshot(tmp_path, b'terraform {}\n')
    address = parse_opentofu_address("unifi_wlan.guest")
    resource = freeze_value(
        {"name": "guest", "nested": {"passphrase": "synthetic-nested-secret"}}
    )
    assert resource.__class__.__name__ == "FrozenObject"
    plan = ReconcilePlan((
        ResourceDecision(
            address,
            Disposition.APPEND,
            ReasonCode.LIVE_RESOURCE_NEW,
            (AppendResource(address, resource), AppendImport(address, "synthetic-id")),
            (),
        ),
    ))

    assert render_reconcile(snapshot=snapshot, plan=plan).files == ()


@pytest.mark.parametrize(
    "owned_source",
    [b"# operator-owned\n", b"# ubitofu: reconcile-preview\n"],
    ids=["unmarked", "malformed-marker"],
)
def test_append_refuses_preexisting_generated_paths_without_a_valid_marker(
    tmp_path, owned_source: bytes
) -> None:
    """Catches appending or formatting a user-owned file selected only by name."""
    from ubitofu.reconcile_renderer import render_reconcile

    snapshot = _snapshot_files(
        tmp_path,
        {"main.tf": b'terraform {}\n', "reconciled_new.tf": owned_source},
    )
    address = parse_opentofu_address("unifi_wlan.guest")
    resource = freeze_value({"name": "guest"})
    assert resource.__class__.__name__ == "FrozenObject"
    plan = ReconcilePlan((
        ResourceDecision(
            address,
            Disposition.APPEND,
            ReasonCode.LIVE_RESOURCE_NEW,
            (AppendResource(address, resource), AppendImport(address, "synthetic-id")),
            (),
        ),
    ))

    assert render_reconcile(snapshot=snapshot, plan=plan).files == ()


def test_append_uses_the_active_tofu_extension_in_a_tofu_module(tmp_path) -> None:
    """Catches generating inactive Terraform compatibility files in a mixed module."""
    from ubitofu.reconcile_renderer import render_reconcile

    snapshot = _snapshot(tmp_path, b'terraform {}\n', name="main.tofu")
    address = parse_opentofu_address("unifi_wlan.guest")
    resource = freeze_value({"name": "guest"})
    assert resource.__class__.__name__ == "FrozenObject"
    plan = ReconcilePlan((
        ResourceDecision(
            address,
            Disposition.APPEND,
            ReasonCode.LIVE_RESOURCE_NEW,
            (AppendResource(address, resource), AppendImport(address, "synthetic-id")),
            (),
        ),
    ))

    preview = render_reconcile(snapshot=snapshot, plan=plan)

    assert preview.changed_paths == (
        PurePosixPath("reconciled_new.tofu"),
        PurePosixPath("unifi-variables.tofu"),
    )


def test_render_returns_no_partial_output_when_generation_is_invalid(tmp_path, monkeypatch) -> None:
    """Catches returning the import or variable candidate after generated HCL fails validation."""
    import ubitofu.reconcile_renderer as renderer

    snapshot = _snapshot(tmp_path, b'terraform {}\n')
    address = parse_opentofu_address("unifi_wlan.guest")
    resource = freeze_value({"name": "guest"})
    assert resource.__class__.__name__ == "FrozenObject"
    plan = ReconcilePlan((
        ResourceDecision(
            address,
            Disposition.APPEND,
            ReasonCode.LIVE_RESOURCE_NEW,
            (AppendResource(address, resource), AppendImport(address, "synthetic-id")),
            (),
        ),
    ))
    monkeypatch.setattr(renderer, "render_resource", lambda *args, **kwargs: "resource {")

    preview = renderer.render_reconcile(snapshot=snapshot, plan=plan)

    assert preview.files == ()


def test_render_returns_no_partial_output_when_owned_formatting_cannot_start(
    tmp_path, monkeypatch
) -> None:
    """Catches an unavailable formatter escaping after the preview has assembled candidates."""
    import ubitofu.reconcile_renderer as renderer

    snapshot = _snapshot(tmp_path, b'terraform {}\n')
    address = parse_opentofu_address("unifi_wlan.guest")
    resource = freeze_value({"name": "guest"})
    assert resource.__class__.__name__ == "FrozenObject"
    plan = ReconcilePlan((
        ResourceDecision(
            address,
            Disposition.APPEND,
            ReasonCode.LIVE_RESOURCE_NEW,
            (AppendResource(address, resource), AppendImport(address, "synthetic-id")),
            (),
        ),
    ))
    def unavailable(*_args, **_kwargs) -> str:
        raise FileNotFoundError

    monkeypatch.setattr(renderer, "render_resource", unavailable)

    assert renderer.render_reconcile(snapshot=snapshot, plan=plan).files == ()


def test_render_rejects_an_append_that_repeats_a_retained_import(tmp_path) -> None:
    """Catches an apparently valid candidate with two import blocks for one address."""
    from ubitofu.reconcile_renderer import render_reconcile

    source = b'import {\n  to = unifi_wlan.guest\n  id = "prior-id"\n}\n'
    snapshot = _snapshot(tmp_path, source)
    address = parse_opentofu_address("unifi_wlan.guest")
    resource = freeze_value({"name": "guest"})
    assert resource.__class__.__name__ == "FrozenObject"
    plan = ReconcilePlan((
        ResourceDecision(
            address,
            Disposition.APPEND,
            ReasonCode.LIVE_RESOURCE_NEW,
            (AppendResource(address, resource), AppendImport(address, "synthetic-id")),
            (),
        ),
    ))

    assert render_reconcile(snapshot=snapshot, plan=plan).files == ()


def test_append_preserves_a_simultaneous_patch_to_the_owned_generated_file(tmp_path) -> None:
    """Catches appending from retained bytes and discarding a patch to the same owned file."""
    from ubitofu.reconcile_renderer import render_reconcile

    source = (
        b"# ubitofu: reconcile-preview v1\n\n"
        b'resource "unifi_network" "lan" { vlan = 10 }\n'
    )
    snapshot = _snapshot(tmp_path, source, name="reconciled_new.tf")
    lan = parse_opentofu_address("unifi_network.lan")
    guest = parse_opentofu_address("unifi_wlan.guest")
    values = freeze_value({"name": "guest"})
    assert values.__class__.__name__ == "FrozenObject"
    plan = ReconcilePlan((
        ResourceDecision(
            lan,
            Disposition.CAPTURE_LIVE,
            ReasonCode.LIVE_ONLY_CHANGE,
            (UpdateScalar(lan, SourceAnchor(lan, ("vlan",), b"10"), b"20"),),
            (),
        ),
        ResourceDecision(
            guest,
            Disposition.APPEND,
            ReasonCode.LIVE_RESOURCE_NEW,
            (AppendResource(guest, values), AppendImport(guest, "synthetic-id")),
            (),
        ),
    ))

    preview = render_reconcile(snapshot=snapshot, plan=plan)
    candidate = {item.relative_path: item.candidate for item in preview.files}[
        PurePosixPath("reconciled_new.tf")
    ]

    assert candidate is not None
    assert b"vlan = 20" in candidate
    assert b'resource "unifi_wlan" "guest"' in candidate


def test_render_add_remove_and_delete_use_indexed_native_spans(tmp_path) -> None:
    """Catches broad textual replacement through comments, repeated blocks, or heredoc decoys."""
    from ubitofu.reconcile_model import AddAttribute, DeleteResource
    from ubitofu.reconcile_renderer import render_reconcile

    lan_block = (
        b'resource "unifi_network" "lan" {\n'
        b'  vlan = 10 # retained\n'
        b'  nested { vlan = 10 }\n'
        b'  note = <<-EOT\n  vlan = 10\n  EOT\n'
        b'}'
    )
    source = lan_block + b'\nresource "unifi_network" "old" { name = "old" }\n'
    snapshot = _snapshot(tmp_path, source)
    lan = parse_opentofu_address("unifi_network.lan")
    old = parse_opentofu_address("unifi_network.old")
    plan = ReconcilePlan((
        ResourceDecision(
            lan,
            Disposition.CAPTURE_LIVE,
            ReasonCode.LIVE_ONLY_CHANGE,
            (
                UpdateScalar(lan, SourceAnchor(lan, ("vlan",), b"10"), b"20"),
                AddAttribute(lan, ("enabled",), SourceAnchor(lan, None, lan_block), b"true"),
            ),
            (),
        ),
        ResourceDecision(
            old,
            Disposition.REMOVE,
            ReasonCode.CONTROLLER_RESOURCE_DELETED,
            (
                DeleteResource(
                    old,
                    SourceAnchor(old, None, b'resource "unifi_network" "old" { name = "old" }'),
                ),
            ),
            (),
        ),
    ))

    preview = render_reconcile(snapshot=snapshot, plan=plan)

    candidate = preview.files[0].candidate
    assert candidate is not None
    assert b"vlan = 20 # retained" in candidate
    assert b"nested { vlan = 10 }" in candidate
    assert b"  vlan = 10\n  EOT" in candidate
    assert b"enabled = true" in candidate
    assert b'resource "unifi_network" "old"' not in candidate
