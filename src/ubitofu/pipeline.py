# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 James Braid
"""Composition root for the public ubitofu commands."""

from __future__ import annotations

from pathlib import Path

from .config import Config
from .controller import Controller, controller_from_config
from .errors import UbitofuError
from .file_transaction import prepare_transaction
from .generate import commit_generate, generate_outcome, prepare_generate
from .health import (
    capture_health,
    compare_health,
    health_snapshot_from_receipt,
    health_snapshot_outcome,
)
from .inspect import inspect_coverage
from .module_index import index_effective_module
from .outcomes import CommandOutcome, read_receipt_file, reconcile_outcome
from .plan_check import check_saved_plan
from .provider_contract import provider_execution
from .reconcile_planner import build_reconcile_plan
from .reconcile_renderer import ReconcilePreview, render_reconcile
from .reconcile_snapshot import collect_reconcile_snapshot
from .runtime import runtime_session
from .tofu_runner import TofuRunner


def prepare_reconcile(
    *,
    cfg: Config,
    controller: Controller,
    runner: TofuRunner,
) -> ReconcilePreview:
    """Collect, classify, and render one immutable reconciliation preview."""
    workdir = Path(cfg.workdir)
    module = index_effective_module(workdir=workdir)
    snapshot = collect_reconcile_snapshot(
        controller=controller,
        runner=runner,
        module=module,
    )
    plan = build_reconcile_plan(snapshot)
    return render_reconcile(snapshot=snapshot, plan=plan)


def run_generate(*, cfg: Config) -> CommandOutcome:
    """Generate and commit one complete adoption preview under one lock."""
    workdir = Path(cfg.workdir)
    with runtime_session(workdir) as session:
        with provider_execution(cfg=cfg, workdir=session.workdir) as execution:
            controller = controller_from_config(cfg)
            try:
                preview = prepare_generate(
                    cfg=cfg,
                    controller=controller,
                    runner=execution.runner(workdir=session.workdir),
                    session=session,
                )
                if not preview.blocked:
                    commit_generate(session=session, preview=preview)
                return generate_outcome(preview)
            finally:
                controller.close()


def run_reconcile(*, cfg: Config, dry_run: bool) -> CommandOutcome:
    """Render one preview and optionally commit that exact candidate set."""
    workdir = Path(cfg.workdir)
    with runtime_session(
        workdir,
        recovery="block" if dry_run else "recover",
    ) as session:
        with provider_execution(cfg=cfg, workdir=session.workdir) as execution:
            controller = controller_from_config(cfg)
            try:
                preview = prepare_reconcile(
                    cfg=cfg,
                    controller=controller,
                    runner=execution.runner(
                        workdir=session.workdir,
                        plan_path=session.plan_path,
                    ),
                )
                if not preview.valid:
                    raise UbitofuError("reconciliation rendering failed")
                outcome = reconcile_outcome(preview)
                if preview.plan.blocked or dry_run:
                    return outcome
                transaction = prepare_transaction(
                    workdir=session.workdir,
                    files=preview.files,
                )
                transaction.commit()
                return outcome
            finally:
                controller.close()


def run_check(*, cfg: Config, plan_path: Path) -> CommandOutcome:
    """Check one supplied saved plan while holding the workdir lock."""
    workdir = Path(cfg.workdir)
    with runtime_session(workdir, recovery="block") as session:
        with provider_execution(cfg=cfg, workdir=session.workdir) as execution:
            controller = controller_from_config(cfg)
            try:
                return check_saved_plan(
                    cfg=cfg,
                    plan_path=plan_path,
                    controller=controller,
                    runner=execution.runner(workdir=session.workdir),
                )
            finally:
                controller.close()


def run_inspect(*, cfg: Config) -> CommandOutcome:
    """Inspect controller/provider coverage without mutating the worktree."""
    workdir = Path(cfg.workdir)
    with provider_execution(cfg=cfg, workdir=workdir) as execution:
        controller = controller_from_config(cfg)
        try:
            return inspect_coverage(
                cfg=cfg,
                controller=controller,
                runner=execution.runner(workdir=workdir),
            )
        finally:
            controller.close()


def run_health_snapshot(*, cfg: Config) -> CommandOutcome:
    """Capture one typed controller health baseline."""
    controller = controller_from_config(cfg)
    try:
        return health_snapshot_outcome(capture_health(controller))
    finally:
        controller.close()


def run_health_compare(*, cfg: Config, before_path: Path) -> CommandOutcome:
    """Compare current health with one supplied private baseline receipt."""
    before = health_snapshot_from_receipt(
        read_receipt_file(before_path, owner_root=Path(cfg.workdir))
    )
    controller = controller_from_config(cfg)
    try:
        return compare_health(before, capture_health(controller))
    finally:
        controller.close()
