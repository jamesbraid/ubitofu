# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 James Braid
from __future__ import annotations

import hashlib
import stat
from pathlib import Path, PurePosixPath

import pytest

from ubitofu.config import Config
from ubitofu.controller import CollectionObservation
from ubitofu.coverage import CoverageReport, Finding, render_coverage_md
from ubitofu.errors import TofuExecutionError, UbitofuError
from ubitofu.generate import (
    GeneratedResource,
    GenerateSnapshot,
    capture_generate_source_identities,
    collect_generate_snapshot,
    commit_generate,
    parse_generated_resources,
    render_generate,
    validate_generate_preview,
)
from ubitofu.module_index import IndexedImport, ModuleIndex, index_effective_module
from ubitofu.outcomes import OutcomeItem
from ubitofu.reconcile_model import ControllerSnapshot, FileIdentity, ProviderSchema
from ubitofu.values import FrozenObject, freeze_value


def _empty_module() -> ModuleIndex:
    return ModuleIndex((), (), (), (), ())


def _frozen_object(value: dict[str, object]) -> FrozenObject:
    frozen = freeze_value(value)
    assert isinstance(frozen, FrozenObject)
    return frozen


def _identity(path: PurePosixPath, source: bytes) -> FileIdentity:
    return FileIdentity(
        path,
        1,
        2,
        stat.S_IFREG | 0o644,
        3,
        4,
        len(source),
        5,
        hashlib.sha256(source).hexdigest(),
    )


def test_generate_snapshot_keeps_only_immutable_reviewed_models() -> None:
    snapshot = GenerateSnapshot(
        controller=ControllerSnapshot((), (), "a" * 64),
        schema=ProviderSchema(()),
        module=_empty_module(),
        imports=(),
        variables=(),
        coverage=(),
        coverage_report=CoverageReport(),
        source_identities=(),
        resources=(),
    )

    with pytest.raises(AttributeError):
        snapshot.imports = ()  # type: ignore[misc]
    with pytest.raises(AttributeError):
        snapshot.coverage_report.gaps = ()  # type: ignore[misc]


def test_failed_generated_plan_removes_a_partial_nonempty_stub(tmp_path: Path) -> None:
    class Controller:
        site = "default"

        def collection(self, endpoint: str) -> list[dict[str, object]]:
            return []

        def collection_observation(self, endpoint: str) -> CollectionObservation:
            return CollectionObservation(endpoint, (), False)

    class Runner:
        workdir = tmp_path

        def plan(
            self, *, out: Path | None = None, generate_config_out: Path | None = None
        ) -> int:
            assert generate_config_out is not None
            generate_config_out.write_bytes(b"partial generated secret")
            raise TofuExecutionError("plan", 1, "execution failed")

    cfg = Config("https://controller.invalid", "default", workdir=str(tmp_path))

    with pytest.raises(TofuExecutionError):
        collect_generate_snapshot(
            cfg=cfg,
            controller=Controller(),  # type: ignore[arg-type]
            runner=Runner(),  # type: ignore[arg-type]
            module=_empty_module(),
        )

    assert not tuple(tmp_path.rglob("generated_stub.tf"))


def test_collect_generate_snapshot_binds_enumerated_imports_to_exact_plan(
    tmp_path: Path,
) -> None:
    class Controller:
        site = "default"

        def collection(self, endpoint: str) -> list[dict[str, object]]:
            if endpoint == "v2/api/site/{site}/bgp/config":
                return [{"_id": "bgp-id", "as_number": 64512}]
            return []

        def collection_observation(self, endpoint: str) -> CollectionObservation:
            records = tuple(
                _frozen_object(item) for item in self.collection(endpoint)
            )
            return CollectionObservation(endpoint, records, False)

    schema = {
        "format_version": "1.0",
        "provider_schemas": {
            "registry.opentofu.org/example/unifi": {
                "resource_schemas": {
                    "unifi_setting": {"block": {"attributes": {}}},
                    "unifi_bgp": {
                        "block": {
                            "attributes": {
                                "as_number": {"type": "number", "optional": True}
                            }
                        }
                    },
                }
            }
        },
    }
    plan = {
        "format_version": "1.2",
        "errored": False,
        "planned_values": {
            "root_module": {
                "resources": [
                    {
                        "address": "unifi_bgp.bgp",
                        "mode": "managed",
                        "type": "unifi_bgp",
                        "name": "bgp",
                        "values": {"as_number": 64512},
                    },
                    {
                        "address": "unifi_setting.setting",
                        "mode": "managed",
                        "type": "unifi_setting",
                        "name": "setting",
                        "values": {},
                    },
                ]
            }
        },
    }

    class Runner:
        workdir = tmp_path
        plan_exit = 2

        def plan(
            self, *, out: Path | None = None, generate_config_out: Path | None = None
        ) -> int:
            assert out is not None and generate_config_out is not None
            out.write_bytes(b"saved plan")
            generate_config_out.write_bytes(b"private generated stub")
            return self.plan_exit

        def show_json(self, plan_file: Path) -> dict[str, object]:
            assert plan_file.read_bytes() == b"saved plan"
            return plan

        def providers_schema(self) -> dict[str, object]:
            return schema

    module = index_effective_module(workdir=tmp_path)
    snapshot = collect_generate_snapshot(
        cfg=Config("https://controller.invalid", "default", workdir=str(tmp_path)),
        controller=Controller(),  # type: ignore[arg-type]
        runner=Runner(),  # type: ignore[arg-type]
        module=module,
    )

    assert tuple(item.address for item in snapshot.imports) == (
        "unifi_bgp.bgp",
        "unifi_setting.setting",
    )
    assert tuple(item.address for item in snapshot.resources) == tuple(
        item.address for item in snapshot.imports
    )
    assert snapshot.coverage_report == CoverageReport()
    assert snapshot.coverage == ()
    assert not tuple(tmp_path.rglob("generated_stub.tf"))


def test_render_generate_builds_one_deterministic_complete_candidate_set(
    tmp_path: Path,
) -> None:
    source = b'terraform { required_version = ">= 1.8" }\n'
    (tmp_path / "main.tf").write_bytes(source)
    module = index_effective_module(workdir=tmp_path)
    resource_schema = _frozen_object(
        {
            "block": {
                "attributes": {
                    "name": {"type": "string", "required": True},
                    "enabled": {"type": "bool", "optional": True},
                    "id": {"type": "string", "computed": True},
                }
            }
        }
    )
    snapshot = GenerateSnapshot(
        controller=ControllerSnapshot((), ("unifi_network",), "a" * 64),
        schema=ProviderSchema((("unifi_network", resource_schema),)),
        module=module,
        imports=(
            IndexedImport(
                "unifi_network.lan", "synthetic-id", PurePosixPath("imports.tf")
            ),
        ),
        variables=(),
        coverage=(),
        coverage_report=CoverageReport(),
        source_identities=(_identity(PurePosixPath("main.tf"), source),),
        resources=(
            GeneratedResource(
                "unifi_network.lan",
                "unifi_network",
                "lan",
                _frozen_object({"name": "lan", "enabled": True, "id": "ignored"}),
            ),
        ),
    )

    preview = render_generate(snapshot)

    assert preview.blocked is False
    assert preview.changed_paths == (
        PurePosixPath("COVERAGE.md"),
        PurePosixPath("generated.tf"),
        PurePosixPath("imports.tf"),
    )
    assert tuple(item.relative_path for item in preview.candidates) == preview.changed_paths
    generated = next(
        item.candidate
        for item in preview.candidates
        if item.relative_path == PurePosixPath("generated.tf")
    )
    assert generated is not None
    assert b'# ubitofu: generated\n' in generated
    assert b'resource "unifi_network" "lan"' in generated
    assert b"id" not in generated
    assert preview.candidate_digests == tuple(
        (item.relative_path, hashlib.sha256(item.candidate or b"").hexdigest())
        for item in preview.candidates
    )


def test_render_generate_models_noops_and_owned_deletions(tmp_path: Path) -> None:
    generated = (
        b'# ubitofu: generated\nresource "unifi_network" "old" {}\n'
    )
    obsolete = b'# ubitofu: generated\nresource "unifi_wlan" "old" {}\n'
    coverage = render_coverage_md(CoverageReport()).encode()
    (tmp_path / "generated.tf").write_bytes(generated)
    (tmp_path / "generated_new.tf").write_bytes(obsolete)
    (tmp_path / "COVERAGE.md").write_bytes(coverage)
    module = index_effective_module(workdir=tmp_path)
    identities = list(capture_generate_source_identities(workdir=tmp_path, module=module))
    coverage_stat = (tmp_path / "COVERAGE.md").stat()
    identities.append(
        FileIdentity(
            PurePosixPath("COVERAGE.md"),
            coverage_stat.st_dev,
            coverage_stat.st_ino,
            coverage_stat.st_mode,
            coverage_stat.st_uid,
            coverage_stat.st_gid,
            coverage_stat.st_size,
            coverage_stat.st_mtime_ns,
            hashlib.sha256(coverage).hexdigest(),
        )
    )
    snapshot = GenerateSnapshot(
        ControllerSnapshot((), (), "a" * 64),
        ProviderSchema(()),
        module,
        (),
        (),
        (),
        CoverageReport(),
        tuple(identities),
        (),
    )

    preview = render_generate(snapshot)

    assert preview.blocked is False
    assert preview.changed_paths == (
        PurePosixPath("generated.tf"),
        PurePosixPath("generated_new.tf"),
    )
    by_path = {item.relative_path: item for item in preview.candidates}
    assert by_path[PurePosixPath("generated.tf")].candidate is None
    assert by_path[PurePosixPath("generated_new.tf")].candidate is None
    assert PurePosixPath("COVERAGE.md") not in by_path


def test_render_generate_reuses_the_canonical_immutable_coverage_report(
    tmp_path: Path,
) -> None:
    module = index_effective_module(workdir=tmp_path)
    report = CoverageReport(
        gaps=(Finding("field", "network.synthetic", "provider schema lacks it"),),
        accepted=(Finding("section", "system", "controller managed"),),
    )
    snapshot = GenerateSnapshot(
        ControllerSnapshot((), (), "a" * 64),
        ProviderSchema(()),
        module,
        (),
        (),
        (),
        report,
        (),
        (),
    )

    preview = render_generate(snapshot)

    coverage = preview.candidates[0]
    assert coverage.relative_path == PurePosixPath("COVERAGE.md")
    assert coverage.candidate == render_coverage_md(report).encode()


def test_generation_blockers_expose_no_committable_candidates(tmp_path: Path) -> None:
    for filename, source in (
        (
            "main.tf",
            b'resource "unifi_network" "lan" { name = "operator-owned" }\n',
        ),
        (
            "main.tf.json",
            b'{"resource":{"unifi_network":{"lan":{"name":"json-owned"}}}}',
        ),
        (
            "generated.tf",
            b'resource "unifi_network" "lan" { name = "unmarked" }\n',
        ),
    ):
        case = tmp_path / filename.replace(".", "-")
        case.mkdir()
        (case / filename).write_bytes(source)
        module = index_effective_module(workdir=case)
        schema = _frozen_object(
            {"block": {"attributes": {"name": {"type": "string", "required": True}}}}
        )
        snapshot = GenerateSnapshot(
            ControllerSnapshot((), ("unifi_network",), "a" * 64),
            ProviderSchema((("unifi_network", schema),)),
            module,
            (
                IndexedImport(
                    "unifi_network.lan", "synthetic-id", PurePosixPath("imports.tf")
                ),
            ),
            (),
            (),
            CoverageReport(),
            capture_generate_source_identities(workdir=case, module=module),
            (
                GeneratedResource(
                    "unifi_network.lan",
                    "unifi_network",
                    "lan",
                    _frozen_object({"name": "lan"}),
                ),
            ),
        )

        preview = render_generate(snapshot)

        assert preview.blocked is True
        assert preview.candidates == ()
        assert preview.changed_paths == ()
        assert preview.findings[0].address is not None
        assert "operator-owned" not in repr(preview.findings)

    coverage_snapshot = GenerateSnapshot(
        ControllerSnapshot((), (), "a" * 64),
        ProviderSchema(()),
        _empty_module(),
        (),
        (),
        (
            OutcomeItem(
                "generation_blocked",
                "blocking",
                None,
                "generation preview is blocked",
            ),
        ),
        CoverageReport(),
        (),
        (),
    )
    assert render_generate(coverage_snapshot).candidates == ()


def test_stale_source_blocks_before_transaction_preparation(tmp_path: Path) -> None:
    source = b'terraform { required_version = ">= 1.8" }\n'
    (tmp_path / "main.tf").write_bytes(source)
    module = index_effective_module(workdir=tmp_path)
    snapshot = GenerateSnapshot(
        ControllerSnapshot((), (), "a" * 64),
        ProviderSchema(()),
        module,
        (),
        (),
        (),
        CoverageReport(),
        capture_generate_source_identities(workdir=tmp_path, module=module),
        (),
    )
    preview = render_generate(snapshot)
    (tmp_path / "main.tf").write_bytes(b'terraform { required_version = ">= 1.9" }\n')

    checked = validate_generate_preview(workdir=tmp_path, preview=preview)

    assert checked.blocked is True
    assert checked.candidates == ()
    assert not (tmp_path / ".ubitofu" / "transactions").exists()


def test_commit_generate_uses_one_transaction_and_rolls_back_injected_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = b'terraform { required_version = ">= 1.8" }\n'
    (tmp_path / "main.tf").write_bytes(source)
    module = index_effective_module(workdir=tmp_path)
    snapshot = GenerateSnapshot(
        ControllerSnapshot((), (), "a" * 64),
        ProviderSchema(()),
        module,
        (),
        (),
        (),
        CoverageReport(),
        capture_generate_source_identities(workdir=tmp_path, module=module),
        (),
    )
    preview = render_generate(snapshot)

    import ubitofu.file_transaction as transaction

    real_apply = transaction._apply_entry
    calls = 0

    def fail_second(*args: object, **kwargs: object) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("synthetic replacement failure")
        real_apply(*args, **kwargs)  # type: ignore[arg-type]

    # Add a second owned candidate so one replacement is rolled back.
    second = preview.candidates[0]
    preview = type(preview)(
        preview.snapshot,
        preview.candidates
        + (
            type(second)(
                PurePosixPath("imports.tf"),
                None,
                b"# ubitofu: generated\n",
                hashlib.sha256(b"# ubitofu: generated\n").hexdigest(),
                second.mode,
            ),
        ),
        preview.changed_paths + (PurePosixPath("imports.tf"),),
        preview.candidate_digests
        + (
            (
                PurePosixPath("imports.tf"),
                hashlib.sha256(b"# ubitofu: generated\n").hexdigest(),
            ),
        ),
        preview.findings,
    )
    monkeypatch.setattr(transaction, "_apply_entry", fail_second)

    with pytest.raises(UbitofuError):
        commit_generate(workdir=tmp_path, preview=preview)

    assert not (tmp_path / "COVERAGE.md").exists()
    assert not (tmp_path / "imports.tf").exists()
    assert (tmp_path / "main.tf").read_bytes() == source


def test_generated_plan_parser_copies_values_and_rejects_duplicate_addresses() -> None:
    plan: dict[str, object] = {
        "format_version": "1.2",
        "errored": False,
        "planned_values": {
            "root_module": {
                "resources": [
                    {
                        "address": "unifi_network.lan",
                        "mode": "managed",
                        "type": "unifi_network",
                        "name": "lan",
                        "values": {"name": "lan", "nested": {"enabled": True}},
                    }
                ]
            }
        },
    }

    parsed = parse_generated_resources(plan)
    values = plan["planned_values"]
    assert isinstance(values, dict)
    root = values["root_module"]
    assert isinstance(root, dict)
    rows = root["resources"]
    assert isinstance(rows, list)
    row = rows[0]
    assert isinstance(row, dict)
    row_values = row["values"]
    assert isinstance(row_values, dict)
    nested = row_values["nested"]
    assert isinstance(nested, dict)
    nested["enabled"] = False
    assert parsed == (
        GeneratedResource(
            "unifi_network.lan",
            "unifi_network",
            "lan",
            _frozen_object({"name": "lan", "nested": {"enabled": True}}),
        ),
    )

    duplicate = {
        **plan,
        "planned_values": {
            "root_module": {"resources": [row, dict(row)]},
        },
    }
    with pytest.raises(ValueError, match="duplicate generated resource"):
        parse_generated_resources(duplicate)


@pytest.mark.parametrize(
    "change",
    [
        {"mode": "data"},
        {"address": "module.child.unifi_network.lan"},
        {"address": "unifi_network.other"},
        {"values": []},
    ],
)
def test_generated_plan_parser_rejects_unsupported_resource_shapes(
    change: dict[str, object],
) -> None:
    row: dict[str, object] = {
        "address": "unifi_network.lan",
        "mode": "managed",
        "type": "unifi_network",
        "name": "lan",
        "values": {"name": "lan"},
    }
    row.update(change)
    plan = {
        "format_version": "1.2",
        "errored": False,
        "planned_values": {"root_module": {"resources": [row]}},
    }

    with pytest.raises(ValueError, match="unsupported generated resource"):
        parse_generated_resources(plan)


def test_source_identity_capture_binds_indexed_bytes_to_filesystem(tmp_path: Path) -> None:
    source = b'resource "unifi_network" "existing" {}\n'
    path = tmp_path / "main.tf"
    path.write_bytes(source)
    module = index_effective_module(workdir=tmp_path)

    identities = capture_generate_source_identities(workdir=tmp_path, module=module)

    assert identities[0].relative_path == PurePosixPath("main.tf")
    assert identities[0].sha256 == hashlib.sha256(source).hexdigest()
    path.write_bytes(b'resource "unifi_network" "changed" {}\n')
    with pytest.raises(ValueError, match="source changed during collection"):
        capture_generate_source_identities(workdir=tmp_path, module=module)
