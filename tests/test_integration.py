# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 James Braid
"""Whole-path integration tests for the 0.10 generation and reconcile cores."""

from __future__ import annotations

import hashlib
import shutil
import subprocess
from pathlib import PurePosixPath

import pytest

from ubitofu.controller_projection import (
    build_controller_snapshot,
    project_controller_snapshot,
)
from ubitofu.coverage import CoverageReport
from ubitofu.enumerator import enumerate_controller
from ubitofu.file_metadata import inspect_file_metadata
from ubitofu.file_transaction import prepare_transaction
from ubitofu.generate import GeneratedResource, GenerateSnapshot, render_generate
from ubitofu.manifest import spec_for_type
from ubitofu.module_index import IndexedImport, ModuleIndex, index_effective_module
from ubitofu.reconcile_model import (
    ActionVector,
    ControllerSnapshot,
    DeleteImport,
    DeleteResource,
    LifecyclePolicy,
    ProviderSchema,
    ReasonCode,
    ReconcileSnapshot,
    ResourceChange,
    ResourceObservation,
    SourceAttribute,
    SourceResource,
    parse_opentofu_address,
)
from ubitofu.reconcile_planner import build_reconcile_plan
from ubitofu.reconcile_renderer import render_reconcile
from ubitofu.reconcile_snapshot import _capture_source_files, normalize_reconcile_snapshot
from ubitofu.tofu_json import parse_plan_document, parse_provider_schema
from ubitofu.values import FrozenObject, freeze_value


def _object(value: dict[str, object]) -> FrozenObject:
    frozen = freeze_value(value)
    assert isinstance(frozen, FrozenObject)
    return frozen


def _state_backed_device_deletion(
    tmp_path, *, unrelated_incomparable: bool
):
    mac = "02:00:00:00:00:01"
    source = (
        f'resource "unifi_device" "deleted" {{\n  mac = "{mac}"\n}}\n'
        f'import {{\n  to = unifi_device.deleted\n  id = "{mac}"\n}}\n'
    ).encode()
    (tmp_path / "main.tf").write_bytes(source)
    module = index_effective_module(workdir=tmp_path)
    values = {"mac": mac}
    plan = parse_plan_document(
        {
            "format_version": "1.2",
            "errored": False,
            "prior_state": {
                "format_version": "1.0",
                "values": {
                    "root_module": {
                        "resources": [
                            {
                                "address": "unifi_device.deleted",
                                "mode": "managed",
                                "type": "unifi_device",
                                "name": "deleted",
                                "values": values,
                                "sensitive_values": {},
                            }
                        ]
                    }
                },
            },
            "resource_changes": [
                {
                    "address": "unifi_device.deleted",
                    "mode": "managed",
                    "type": "unifi_device",
                    "name": "deleted",
                    "change": {
                        "actions": ["create"],
                        "before": None,
                        "after": values,
                        "after_unknown": {},
                        "before_sensitive": False,
                        "after_sensitive": {},
                    },
                }
            ],
            "resource_drift": [
                {
                    "address": "unifi_device.deleted",
                    "mode": "managed",
                    "type": "unifi_device",
                    "name": "deleted",
                    "change": {
                        "actions": ["delete"],
                        "before": values,
                        "after": None,
                        "after_unknown": {},
                        "before_sensitive": {},
                        "after_sensitive": False,
                    },
                }
            ],
        }
    )
    schema = parse_provider_schema(
        {
            "format_version": "1.0",
            "provider_schemas": {
                "registry.opentofu.org/example/unifi": {
                    "resource_schemas": {
                        "unifi_device": {
                            "block": {
                                "attributes": {
                                    "mac": {"type": "string", "required": True},
                                    "name": {"type": "string", "required": True},
                                }
                            }
                        }
                    }
                }
            },
        }
    )

    class Controller:
        site = "default"

        def collection(self, endpoint: str) -> list[dict[str, object]]:
            assert endpoint == "stat/device"
            records: list[dict[str, object]] = [
                {"mac": mac, "name": "forgotten", "adopted": False}
            ]
            if unrelated_incomparable:
                records.append({"mac": "02:00:00:00:00:02", "adopted": True})
            return records

    enumeration = enumerate_controller(
        Controller(),  # type: ignore[arg-type]
        manifest=[spec_for_type("unifi_device")],
        capture_records=True,
    )
    assert [item.import_id for item in enumeration.records] == (
        ["02:00:00:00:00:02"] if unrelated_incomparable else []
    )
    assert enumeration.gaps == []
    assert [(item.reason, item.count) for item in enumeration.accepted_exclusions] == [
        ("unadopted_device", 1)
    ]
    controller = build_controller_snapshot(
        records=tuple(
            (
                record.resource_type,
                record.import_id,
                {key: value for key, value in record.raw.items},
            )
            for record in enumeration.records
        ),
        covered_resource_types=tuple(enumeration.covered_resource_types),
        name_hints={
            (target.resource_type, target.import_id): target.name_hint
            for target in enumeration.targets
        },
    )
    projection = project_controller_snapshot(
        plan=plan,
        controller=controller,
        schema=schema,
    )
    normalized = normalize_reconcile_snapshot(
        plan=plan,
        schema=schema,
        live=projection,
        module=module,
    )
    snapshot = _capture_source_files(normalized, tmp_path)
    reconcile_plan = build_reconcile_plan(snapshot)
    preview = render_reconcile(snapshot=snapshot, plan=reconcile_plan)
    return reconcile_plan, preview


def test_state_backed_unadopted_device_deletion_removes_native_resource_and_import(
    tmp_path,
) -> None:
    plan, preview = _state_backed_device_deletion(
        tmp_path, unrelated_incomparable=False
    )

    assert plan.blocked is False
    assert [type(edit) for edit in plan.edits] == [DeleteImport, DeleteResource]
    assert preview.changed_paths == (PurePosixPath("main.tf"),)
    assert len(preview.files) == 1
    candidate = preview.files[0].candidate
    assert candidate is not None
    assert b'unifi_device" "deleted' not in candidate
    assert b"import {" not in candidate


def test_unrelated_local_blocker_retains_device_deletion_reason_but_suppresses_writes(
    tmp_path,
) -> None:
    plan, preview = _state_backed_device_deletion(
        tmp_path, unrelated_incomparable=True
    )

    assert plan.blocked is True
    assert plan.edits == ()
    assert {decision.reason for decision in plan.decisions} == {
        ReasonCode.CONTROLLER_RESOURCE_DELETED,
        ReasonCode.INCOMPARABLE_CONTROLLER_OBSERVATION,
    }
    assert preview.changed_paths == ()
    assert preview.files == ()


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


@pytest.mark.skipif(shutil.which("tofu") is None, reason="OpenTofu is unavailable")
def test_controller_deletion_commits_resource_and_import_removal_that_tofu_112_validates(
    tmp_path,
):
    source = (
        b'terraform { required_version = ">= 1.12.0" }\n'
        b'resource "terraform_data" "deleted" { input = "synthetic" }\n'
        b'import { to = terraform_data.deleted id = "synthetic-id" }\n'
    )
    path = tmp_path / "main.tf"
    path.write_bytes(source)
    module = index_effective_module(workdir=tmp_path)
    identity = inspect_file_metadata(path, relative_path=PurePosixPath("main.tf")).identity
    address = parse_opentofu_address("terraform_data.deleted")
    values = _object({"input": "synthetic"})
    committed = SourceResource(
        address,
        identity,
        b'resource "terraform_data" "deleted" { input = "synthetic" }',
        values,
        (SourceAttribute(("input",), b'input = "synthetic"', b'"synthetic"', True),),
    )
    observation = ResourceObservation(
        address=address,
        committed=committed,
        base=values,
        desired=values,
        live=None,
        change=ResourceChange(address, ActionVector.CREATE, None, values, FrozenObject(())),
        lifecycle=LifecyclePolicy(False, "capture"),
        collection_identities=(),
        fresh_present=False,
    )
    snapshot = ReconcileSnapshot((observation,), module, (identity,), "c" * 64)

    plan = build_reconcile_plan(snapshot)
    preview = render_reconcile(snapshot=snapshot, plan=plan)
    prepare_transaction(workdir=tmp_path, files=preview.files).commit()

    retained = path.read_bytes()
    assert b'terraform_data" "deleted' not in retained
    assert b"import {" not in retained
    version = subprocess.run(
        ["tofu", "version"], check=True, capture_output=True, text=True
    ).stdout
    assert version.startswith("OpenTofu v1.12.")
    validated = subprocess.run(
        ["tofu", "validate", "-no-color"], cwd=tmp_path, capture_output=True, text=True
    )
    assert validated.returncode == 0, validated.stdout + validated.stderr
