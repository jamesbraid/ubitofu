# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 James Braid
"""Composition tests for the 0.10 command pipeline."""

from __future__ import annotations

import hashlib
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from types import SimpleNamespace

import pytest

import ubitofu.pipeline as pipeline
from ubitofu.config import Config
from ubitofu.errors import UbitofuError
from ubitofu.file_metadata import inspect_file_metadata
from ubitofu.file_transaction import prepare_transaction
from ubitofu.outcomes import CommandOutcome, OutcomeItem
from ubitofu.reconcile_renderer import ProposedFile
from ubitofu.values import freeze_value


def _cfg(tmp_path: Path) -> Config:
    return Config(
        "https://unifi.example",
        "default",
        "env",
        "UNIFI_API_KEY",
        workdir=str(tmp_path),
    )


def _outcome(*, command: str = "reconcile", blocked: bool = False) -> CommandOutcome:
    if command == "reconcile":
        items = (
            OutcomeItem(
                "concurrent_value_conflict",
                "blocking",
                None,
                "concurrent values conflict",
            ),
        ) if blocked else ()
        return CommandOutcome(
            command,
            True,
            blocked,
            "reconciliation complete",
            items,
            (("active_source", "a" * 64), ("controller", "b" * 64)),
            freeze_value(
                {
                    "changed_paths": ["main.tf"],
                    "candidate_digests": [["main.tf", "c" * 64]],
                }
            ),
        )
    return CommandOutcome(
        "generate",
        True,
        False,
        "generation preview complete",
        (),
        (
            ("active_source", "a" * 64),
            ("controller", "b" * 64),
            ("provider_schema", "c" * 64),
        ),
        freeze_value(
            {
                "changed_paths": ["generated.tf"],
                "candidate_digests": [["generated.tf", "d" * 64]],
            }
        ),
    )


class _Controller:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


def _session(tmp_path: Path):
    return SimpleNamespace(workdir=tmp_path, plan_path=tmp_path / "private.tfplan")


def test_prepare_reconcile_composes_one_snapshot_plan_and_preview(monkeypatch, tmp_path):
    calls: list[tuple[str, object]] = []
    module = object()
    snapshot = object()
    plan = object()
    preview = object()
    controller = object()
    runner = SimpleNamespace(workdir=tmp_path)

    monkeypatch.setattr(
        pipeline,
        "index_effective_module",
        lambda *, workdir: calls.append(("index", workdir)) or module,
    )
    monkeypatch.setattr(
        pipeline,
        "collect_reconcile_snapshot",
        lambda *, controller, runner, module: calls.append(("snapshot", module)) or snapshot,
    )
    monkeypatch.setattr(
        pipeline,
        "build_reconcile_plan",
        lambda value: calls.append(("plan", value)) or plan,
    )
    monkeypatch.setattr(
        pipeline,
        "render_reconcile",
        lambda *, snapshot, plan: calls.append(("render", (snapshot, plan))) or preview,
    )

    result = pipeline.prepare_reconcile(
        cfg=_cfg(tmp_path), controller=controller, runner=runner
    )

    assert result is preview
    assert calls == [
        ("index", tmp_path),
        ("snapshot", module),
        ("plan", snapshot),
        ("render", (snapshot, plan)),
    ]


def test_reconcile_wet_and_dry_consume_identical_immutable_preview(monkeypatch, tmp_path):
    controller = _Controller()
    snapshot = object()
    plan = SimpleNamespace(blocked=False, decisions=(object(),))
    candidate = SimpleNamespace(relative_path=PurePosixPath("main.tf"), candidate=b"after\n")
    preview = SimpleNamespace(
        valid=True,
        snapshot=snapshot,
        plan=plan,
        files=(candidate,),
        changed_paths=(PurePosixPath("main.tf"),),
        candidate_digests=((PurePosixPath("main.tf"), "c" * 64),),
    )
    prepared: list[tuple[Path, tuple[object, ...]]] = []
    committed: list[object] = []
    rendered: list[tuple[object, object, bytes, object, object]] = []

    @contextmanager
    def fake_session(workdir, **kwargs):
        yield _session(workdir)

    class Transaction:
        def commit(self):
            committed.append(self)

    monkeypatch.setattr(pipeline, "runtime_session", fake_session)
    monkeypatch.setattr(pipeline, "controller_from_config", lambda cfg: controller)
    monkeypatch.setattr(
        pipeline, "TofuRunner", lambda workdir, **kwargs: SimpleNamespace(workdir=workdir)
    )
    monkeypatch.setattr(pipeline, "prepare_reconcile", lambda **kwargs: preview)
    def record_outcome(value):
        rendered.append(
            (
                value.snapshot,
                value.plan,
                value.files[0].candidate,
                value.changed_paths,
                value.candidate_digests,
            )
        )
        return _outcome()

    monkeypatch.setattr(pipeline, "reconcile_outcome", record_outcome)
    monkeypatch.setattr(
        pipeline,
        "prepare_transaction",
        lambda *, workdir, files: prepared.append((workdir, files)) or Transaction(),
    )

    dry = pipeline.run_reconcile(cfg=_cfg(tmp_path), dry_run=True)
    wet = pipeline.run_reconcile(cfg=_cfg(tmp_path), dry_run=False)

    assert dry == wet == _outcome()
    assert rendered == [
        (snapshot, plan, b"after\n", preview.changed_paths, preview.candidate_digests),
        (snapshot, plan, b"after\n", preview.changed_paths, preview.candidate_digests),
    ]
    assert controller.closed is True
    assert prepared == [(tmp_path, (candidate,))]
    assert len(committed) == 1


def test_dry_run_never_prepares_a_transaction_or_changes_destination(monkeypatch, tmp_path):
    destination = tmp_path / "main.tf"
    destination.write_bytes(b"before\n")
    before = destination.read_bytes()
    preview = SimpleNamespace(
        valid=True, plan=SimpleNamespace(blocked=False), files=(object(),)
    )

    @contextmanager
    def fake_session(workdir, **kwargs):
        yield _session(workdir)

    monkeypatch.setattr(pipeline, "runtime_session", fake_session)
    monkeypatch.setattr(pipeline, "controller_from_config", lambda cfg: _Controller())
    monkeypatch.setattr(pipeline, "TofuRunner", lambda workdir, **kwargs: object())
    monkeypatch.setattr(pipeline, "prepare_reconcile", lambda **kwargs: preview)
    monkeypatch.setattr(pipeline, "reconcile_outcome", lambda value: _outcome())
    monkeypatch.setattr(
        pipeline,
        "prepare_transaction",
        lambda **kwargs: pytest.fail("dry-run prepared a transaction"),
    )

    pipeline.run_reconcile(cfg=_cfg(tmp_path), dry_run=True)

    assert destination.read_bytes() == before


def test_blocking_reconcile_never_prepares_a_transaction(monkeypatch, tmp_path):
    preview = SimpleNamespace(valid=True, plan=SimpleNamespace(blocked=True), files=())

    @contextmanager
    def fake_session(workdir, **kwargs):
        yield _session(workdir)

    monkeypatch.setattr(pipeline, "runtime_session", fake_session)
    monkeypatch.setattr(pipeline, "controller_from_config", lambda cfg: _Controller())
    monkeypatch.setattr(pipeline, "TofuRunner", lambda workdir, **kwargs: object())
    monkeypatch.setattr(pipeline, "prepare_reconcile", lambda **kwargs: preview)
    monkeypatch.setattr(pipeline, "reconcile_outcome", lambda value: _outcome(blocked=True))
    monkeypatch.setattr(
        pipeline,
        "prepare_transaction",
        lambda **kwargs: pytest.fail("blocked plan prepared a transaction"),
    )

    assert pipeline.run_reconcile(cfg=_cfg(tmp_path), dry_run=False).blocked is True


@pytest.mark.parametrize("dry_run", [False, True])
def test_invalid_render_fails_closed_before_transaction(monkeypatch, tmp_path, dry_run):
    preview = SimpleNamespace(
        valid=False,
        plan=SimpleNamespace(blocked=False),
        files=(),
    )

    @contextmanager
    def fake_session(workdir, **kwargs):
        yield _session(workdir)

    monkeypatch.setattr(pipeline, "runtime_session", fake_session)
    monkeypatch.setattr(pipeline, "controller_from_config", lambda cfg: _Controller())
    monkeypatch.setattr(pipeline, "TofuRunner", lambda workdir, **kwargs: object())
    monkeypatch.setattr(pipeline, "prepare_reconcile", lambda **kwargs: preview)
    monkeypatch.setattr(
        pipeline,
        "prepare_transaction",
        lambda **kwargs: pytest.fail("invalid render prepared a transaction"),
    )

    with pytest.raises(UbitofuError):
        pipeline.run_reconcile(cfg=_cfg(tmp_path), dry_run=dry_run)


def test_reconcile_binds_saved_plan_to_the_runtime_session(monkeypatch, tmp_path):
    controller = _Controller()
    session = SimpleNamespace(workdir=tmp_path, plan_path=tmp_path / "private.tfplan")
    captured = {}
    preview = SimpleNamespace(
        valid=True,
        plan=SimpleNamespace(blocked=False),
        files=(),
    )

    @contextmanager
    def fake_session(workdir, **kwargs):
        yield session

    class Runner:
        def __init__(self, *, workdir, plan_path):
            captured["workdir"] = workdir
            captured["plan_path"] = plan_path

    def prepare(**kwargs):
        assert kwargs["runner"].plan_path == session.plan_path
        return preview

    Runner.plan_path = session.plan_path

    @contextmanager
    def fake_execution(**kwargs):
        yield SimpleNamespace(
            runner=lambda *, workdir, plan_path=None: Runner(
                workdir=workdir,
                plan_path=plan_path,
            )
        )

    monkeypatch.setattr(pipeline, "runtime_session", fake_session)
    monkeypatch.setattr(pipeline, "controller_from_config", lambda cfg: controller)
    monkeypatch.setattr(pipeline, "provider_execution", fake_execution)
    monkeypatch.setattr(pipeline, "prepare_reconcile", prepare)
    monkeypatch.setattr(pipeline, "reconcile_outcome", lambda value: _outcome())
    monkeypatch.setattr(
        pipeline,
        "prepare_transaction",
        lambda **kwargs: SimpleNamespace(commit=lambda: None),
    )

    pipeline.run_reconcile(cfg=_cfg(tmp_path), dry_run=True)

    assert captured == {"workdir": tmp_path, "plan_path": session.plan_path}


def test_generate_commits_the_exact_preview_without_reconstruction(monkeypatch, tmp_path):
    controller = _Controller()
    preview = SimpleNamespace(blocked=False)
    committed: list[tuple[object, object]] = []

    @contextmanager
    def fake_session(workdir, **kwargs):
        yield _session(workdir)

    monkeypatch.setattr(pipeline, "runtime_session", fake_session)
    monkeypatch.setattr(pipeline, "controller_from_config", lambda cfg: controller)
    monkeypatch.setattr(
        pipeline, "TofuRunner", lambda workdir, **kwargs: SimpleNamespace(workdir=workdir)
    )
    monkeypatch.setattr(pipeline, "prepare_generate", lambda **kwargs: preview)
    monkeypatch.setattr(
        pipeline,
        "commit_generate",
        lambda *, session, preview: committed.append((session, preview)),
    )
    monkeypatch.setattr(pipeline, "generate_outcome", lambda value: _outcome(command="generate"))

    outcome = pipeline.run_generate(cfg=_cfg(tmp_path))

    assert outcome == _outcome(command="generate")
    assert committed[0][1] is preview
    assert controller.closed is True


def test_blocked_generate_returns_the_exact_preview_without_commit(monkeypatch, tmp_path):
    preview = SimpleNamespace(blocked=True)

    @contextmanager
    def fake_session(workdir, **kwargs):
        yield _session(workdir)

    monkeypatch.setattr(pipeline, "runtime_session", fake_session)
    monkeypatch.setattr(pipeline, "controller_from_config", lambda cfg: _Controller())
    monkeypatch.setattr(pipeline, "TofuRunner", lambda workdir, **kwargs: object())
    monkeypatch.setattr(pipeline, "prepare_generate", lambda **kwargs: preview)
    monkeypatch.setattr(
        pipeline,
        "commit_generate",
        lambda **kwargs: pytest.fail("blocked generation committed"),
    )
    blocked = CommandOutcome(
        "generate",
        False,
        True,
        "generation preview complete",
        (
            OutcomeItem(
                "generation_blocked",
                "blocking",
                None,
                "generation preview is blocked",
            ),
        ),
        (
            ("active_source", "a" * 64),
            ("controller", "b" * 64),
            ("provider_schema", "c" * 64),
        ),
        freeze_value({"changed_paths": [], "candidate_digests": []}),
    )
    monkeypatch.setattr(pipeline, "generate_outcome", lambda value: blocked)

    assert pipeline.run_generate(cfg=_cfg(tmp_path)) is blocked


def test_pipeline_propagates_failures_and_closes_controller(monkeypatch, tmp_path):
    controller = _Controller()

    @contextmanager
    def fake_session(workdir, **kwargs):
        yield _session(workdir)

    monkeypatch.setattr(pipeline, "runtime_session", fake_session)
    monkeypatch.setattr(pipeline, "controller_from_config", lambda cfg: controller)
    monkeypatch.setattr(pipeline, "TofuRunner", lambda workdir, **kwargs: object())
    monkeypatch.setattr(
        pipeline,
        "prepare_reconcile",
        lambda **kwargs: (_ for _ in ()).throw(RuntimeError("snapshot failed")),
    )

    with pytest.raises(RuntimeError, match="snapshot failed"):
        pipeline.run_reconcile(cfg=_cfg(tmp_path), dry_run=False)
    assert controller.closed is True


def test_runtime_cleanup_failure_replaces_a_successful_result(monkeypatch, tmp_path):
    controller = _Controller()
    preview = SimpleNamespace(valid=True, plan=SimpleNamespace(blocked=False), files=())

    @contextmanager
    def failing_cleanup(workdir, **kwargs):
        yield _session(workdir)
        raise RuntimeError("cleanup failed")

    monkeypatch.setattr(pipeline, "runtime_session", failing_cleanup)
    monkeypatch.setattr(pipeline, "controller_from_config", lambda cfg: controller)
    monkeypatch.setattr(pipeline, "TofuRunner", lambda workdir, **kwargs: object())
    monkeypatch.setattr(pipeline, "prepare_reconcile", lambda **kwargs: preview)
    monkeypatch.setattr(pipeline, "reconcile_outcome", lambda value: _outcome())

    with pytest.raises(RuntimeError, match="cleanup failed"):
        pipeline.run_reconcile(cfg=_cfg(tmp_path), dry_run=True)
    assert controller.closed is True


def test_dry_run_blocks_transaction_residue_without_recovering_hcl(tmp_path):
    destination = tmp_path / "main.tf"
    destination.write_bytes(b"old bytes\n")
    identity = inspect_file_metadata(
        destination, relative_path=PurePosixPath("main.tf")
    ).identity
    candidate = b"new bytes\n"
    transaction = prepare_transaction(
        workdir=tmp_path,
        files=(
            ProposedFile(
                PurePosixPath("main.tf"),
                identity,
                candidate,
                hashlib.sha256(candidate).hexdigest(),
                identity.mode,
            ),
        ),
    )
    manifest = transaction.transaction_root / "manifest.json"
    before = (destination.read_bytes(), manifest.read_bytes())

    with pytest.raises(UbitofuError):
        pipeline.run_reconcile(cfg=_cfg(tmp_path), dry_run=True)

    assert transaction.transaction_root.exists()
    assert (destination.read_bytes(), manifest.read_bytes()) == before


def test_dry_run_blocks_scaffold_residue_without_deleting_it(tmp_path):
    destination = tmp_path / "main.tf"
    destination.write_bytes(b"source bytes\n")
    scaffold = tmp_path / "ubitofu-imports.tf"
    scaffold.write_bytes(b"crash residue\n")
    before = (destination.read_bytes(), scaffold.read_bytes())

    with pytest.raises(UbitofuError):
        pipeline.run_reconcile(cfg=_cfg(tmp_path), dry_run=True)

    assert (destination.read_bytes(), scaffold.read_bytes()) == before


def test_check_blocks_transaction_residue_without_recovering_hcl(tmp_path):
    destination = tmp_path / "main.tf"
    destination.write_bytes(b"old bytes\n")
    identity = inspect_file_metadata(
        destination, relative_path=PurePosixPath("main.tf")
    ).identity
    candidate = b"new bytes\n"
    transaction = prepare_transaction(
        workdir=tmp_path,
        files=(
            ProposedFile(
                PurePosixPath("main.tf"),
                identity,
                candidate,
                hashlib.sha256(candidate).hexdigest(),
                identity.mode,
            ),
        ),
    )
    manifest = transaction.transaction_root / "manifest.json"
    before = (destination.read_bytes(), manifest.read_bytes())

    with pytest.raises(UbitofuError):
        pipeline.run_check(cfg=_cfg(tmp_path), plan_path=tmp_path / "saved.tfplan")

    assert transaction.transaction_root.exists()
    assert (destination.read_bytes(), manifest.read_bytes()) == before


@pytest.mark.parametrize("kind", ["symlink", "fifo", "oversized", "public"])
def test_health_baseline_reader_rejects_unsafe_or_unbounded_files(
    monkeypatch, tmp_path, kind
):
    before = tmp_path / "before.json"
    if kind == "symlink":
        target = tmp_path / "target.json"
        target.write_bytes(b"{}")
        target.chmod(0o600)
        before.symlink_to(target)
    elif kind == "fifo":
        import os

        os.mkfifo(before, 0o600)
    elif kind == "oversized":
        before.write_bytes(b"x" * (128 * 1024 + 1))
        before.chmod(0o600)
    else:
        before.write_bytes(b"{}")
        before.chmod(0o644)
    monkeypatch.setattr(
        pipeline,
        "controller_from_config",
        lambda cfg: pytest.fail("unsafe baseline reached the controller"),
    )

    with pytest.raises(UbitofuError):
        pipeline.run_health_compare(cfg=_cfg(tmp_path), before_path=before)
