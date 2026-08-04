# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 James Braid
"""Whole-path integration tests for the 0.10 generation and reconcile cores."""

from __future__ import annotations

import hashlib
from pathlib import PurePosixPath

from ubitofu.coverage import CoverageReport
from ubitofu.file_metadata import inspect_file_metadata
from ubitofu.file_transaction import prepare_transaction
from ubitofu.generate import GeneratedResource, GenerateSnapshot, render_generate
from ubitofu.module_index import IndexedImport, ModuleIndex, index_effective_module
from ubitofu.reconcile_model import (
    ActionVector,
    ControllerSnapshot,
    LifecyclePolicy,
    ProviderSchema,
    ReconcileSnapshot,
    ResourceChange,
    ResourceObservation,
    SourceAttribute,
    SourceResource,
    parse_opentofu_address,
)
from ubitofu.reconcile_planner import build_reconcile_plan
from ubitofu.reconcile_renderer import render_reconcile
from ubitofu.values import FrozenObject, freeze_value


def _object(value: dict[str, object]) -> FrozenObject:
    frozen = freeze_value(value)
    assert isinstance(frozen, FrozenObject)
    return frozen


def test_generation_snapshot_renders_secret_safe_module_and_commits_once(tmp_path):
    secret = "plain-controller-passphrase"
    address = "unifi_network.generated"
    schema = _object(
        {
            "block": {
                "attributes": {
                    "name": {"type": "string", "required": True},
                    "x_passphrase": {"type": "string", "optional": True},
                }
            }
        }
    )
    snapshot = GenerateSnapshot(
        controller=ControllerSnapshot((), ("unifi_network",), "a" * 64),
        schema=ProviderSchema((("unifi_network", schema),)),
        module=ModuleIndex((), (), (), (), ()),
        imports=(IndexedImport(address, "network-id", PurePosixPath("imports.tf")),),
        variables=(),
        coverage=(),
        coverage_report=CoverageReport(),
        source_identities=(),
        resources=(
            GeneratedResource(
                address,
                "unifi_network",
                "generated",
                _object({"name": "generated", "x_passphrase": secret}),
            ),
        ),
    )

    preview = render_generate(snapshot)
    assert preview.blocked is False
    assert secret.encode() not in b"".join(
        file.candidate or b"" for file in preview.candidates
    )
    transaction = prepare_transaction(workdir=tmp_path, files=preview.candidates)
    transaction.commit()

    generated = (tmp_path / "generated.tf").read_bytes()
    assert b'resource "unifi_network" "generated"' in generated
    assert b"ignore_changes" in generated
    assert (tmp_path / "imports.tf").is_file()
    assert (tmp_path / "COVERAGE.md").is_file()


def test_reconcile_snapshot_plans_renders_and_commits_live_scalar_change(tmp_path):
    source = b'resource "unifi_network" "lan" {\n  vlan = 10\n}\n'
    path = tmp_path / "main.tf"
    path.write_bytes(source)
    module = index_effective_module(workdir=tmp_path)
    identity = inspect_file_metadata(
        path, relative_path=PurePosixPath("main.tf")
    ).identity
    address = parse_opentofu_address("unifi_network.lan")
    base = _object({"vlan": 10})
    live = _object({"vlan": 20})
    committed = SourceResource(
        address,
        identity,
        source.rstrip(b"\n"),
        base,
        (SourceAttribute(("vlan",), b"vlan = 10", b"10", True),),
    )
    observation = ResourceObservation(
        address=address,
        committed=committed,
        base=base,
        desired=base,
        live=live,
        change=ResourceChange(
            address,
            ActionVector.UPDATE,
            live,
            base,
            FrozenObject(()),
        ),
        lifecycle=LifecyclePolicy(False, "capture"),
        collection_identities=(),
        fresh_present=True,
        fresh=live,
        import_id="network-id",
    )
    snapshot = ReconcileSnapshot((observation,), module, (identity,), "b" * 64)

    plan = build_reconcile_plan(snapshot)
    preview = render_reconcile(snapshot=snapshot, plan=plan)

    assert plan.blocked is False
    assert preview.valid is True
    assert preview.changed_paths == (PurePosixPath("main.tf"),)
    assert preview.candidate_digests == (
        (PurePosixPath("main.tf"), hashlib.sha256(preview.files[0].candidate or b"").hexdigest()),
    )
    transaction = prepare_transaction(workdir=tmp_path, files=preview.files)
    transaction.commit()
    assert b"vlan = 20" in path.read_bytes()
