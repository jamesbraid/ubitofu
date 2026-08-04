# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 James Braid
"""Point-in-time authorization of one exact saved OpenTofu plan."""

from __future__ import annotations

import hashlib
import os
import stat
from dataclasses import dataclass
from pathlib import Path

from .config import Config
from .controller import Controller
from .controller_projection import build_controller_snapshot, project_controller_snapshot
from .enumerator import enumerate_controller
from .errors import UbitofuError
from .module_index import ModuleIndex, index_effective_module
from .outcomes import (
    COMMAND_PROFILES,
    CommandOutcome,
    OutcomeItem,
    digest_active_source,
    digest_controller_observations,
    opaque_reference,
)
from .reconcile_model import (
    ActionVector,
    ControllerSnapshot,
    Disposition,
    ReasonCode,
    ReconcilePlan,
    ReconcileSnapshot,
)
from .reconcile_planner import build_reconcile_plan
from .reconcile_snapshot import normalize_reconcile_snapshot
from .tofu_json import parse_plan_document, parse_provider_schema
from .tofu_runner import TofuRunner
from .values import FrozenObject, FrozenValue


@dataclass(frozen=True)
class _PlanFileFacts:
    device: int
    inode: int
    mode: int
    uid: int
    gid: int
    size: int
    mtime_ns: int
    ctime_ns: int


@dataclass
class _OpenedPlan:
    path: Path
    fd: int
    facts: _PlanFileFacts
    content_sha256: str

    def close(self) -> None:
        os.close(self.fd)


def check_saved_plan(
    *,
    cfg: Config,
    plan_path: Path,
    controller: Controller,
    runner: TofuRunner,
) -> CommandOutcome:
    """Authorize one saved plan against a fresh, provider-shaped controller view.

    Controller endpoints are read once, sequentially, as the final controller
    observation. Without a revision token those reads span a collection window:
    a value can change after its endpoint was read, including before this function
    returns. The saved plan is rechecked after semantic processing. The caller
    must check immediately before apply and verify the plan digest again.
    """
    workdir = Path(cfg.workdir)
    if workdir != runner.workdir.resolve():
        raise UbitofuError("saved-plan check workdir mismatch")
    opened = _open_plan(_filesystem_plan_path(plan_path, runner.workdir))
    try:
        raw_plan = runner.show_json(opened.path)
        _recheck_plan(opened)
        plan = parse_plan_document(raw_plan)
        schema = parse_provider_schema(runner.providers_schema())
        module = index_effective_module(workdir=workdir)
        controller_snapshot = _collect_controller_snapshot(controller)
        projection = project_controller_snapshot(
            plan=plan,
            controller=controller_snapshot,
            schema=schema,
        )
        snapshot = normalize_reconcile_snapshot(
            plan=plan,
            schema=schema,
            live=projection,
            module=module,
        )
        semantic = build_reconcile_plan(snapshot)
        _recheck_plan(opened)
    finally:
        opened.close()

    blocked = _check_is_blocked(semantic)
    items = [
        _decision_item(decision.reason, decision.address.absolute)
        for decision in semantic.decisions
    ]
    items.extend(
        _profile_item(ReasonCode.SECRET_FRESHNESS_UNVERIFIED.value, observation.address.absolute)
        for observation in snapshot.resources
        if observation.secret_changes
        and observation.change is not None
        and observation.change.action is not ActionVector.NOOP
    )
    items.append(_profile_item("unsafe_plan" if blocked else "plan_allowed"))
    return CommandOutcome(
        command="check",
        changed=any(change.action is not ActionVector.NOOP for change in plan.changes),
        blocked=blocked,
        summary="saved plan check complete",
        items=tuple(items),
        input_digests=(
            ("saved_plan", opened.content_sha256),
            ("active_source", _active_source_digest(module)),
            ("plan_time_live", _plan_time_live_digest(snapshot)),
            ("fresh_controller", projection.canonical_sha256),
        ),
        payload=None,
    )


def _filesystem_plan_path(plan_path: Path, workdir: Path) -> Path:
    candidate = plan_path if plan_path.is_absolute() else workdir / plan_path
    return candidate.absolute()


def _open_plan(path: Path) -> _OpenedPlan:
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise UbitofuError("saved plan is unavailable") from exc
    try:
        facts = _plan_facts(os.fstat(fd))
        _validate_plan_facts(facts)
        digest = _descriptor_digest(fd)
    except BaseException:
        os.close(fd)
        raise
    return _OpenedPlan(path, fd, facts, digest)


def _plan_facts(value: os.stat_result) -> _PlanFileFacts:
    return _PlanFileFacts(
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_uid,
        value.st_gid,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def _validate_plan_facts(facts: _PlanFileFacts) -> None:
    if not stat.S_ISREG(facts.mode) or facts.size == 0:
        raise UbitofuError("saved plan must be one nonempty regular file")


def _descriptor_digest(fd: int) -> str:
    digest = hashlib.sha256()
    os.lseek(fd, 0, os.SEEK_SET)
    while chunk := os.read(fd, 1024 * 1024):
        digest.update(chunk)
    return digest.hexdigest()


def _recheck_plan(opened: _OpenedPlan) -> None:
    try:
        retained_facts = _plan_facts(os.fstat(opened.fd))
        retained_digest = _descriptor_digest(opened.fd)
        reopened = _open_plan(opened.path)
    except (OSError, UbitofuError) as exc:
        raise UbitofuError("saved plan changed during check") from exc
    try:
        if (
            retained_facts != opened.facts
            or reopened.facts != opened.facts
            or retained_digest != opened.content_sha256
            or reopened.content_sha256 != opened.content_sha256
        ):
            raise UbitofuError("saved plan changed during check")
    finally:
        reopened.close()


def _collect_controller_snapshot(controller: Controller) -> ControllerSnapshot:
    enumeration = enumerate_controller(controller, capture_records=True)
    records = tuple(
        (record.resource_type, record.import_id, _thaw_object(record.raw))
        for record in enumeration.records
    )
    hints = {
        (target.resource_type, target.import_id): target.name_hint
        for target in enumeration.targets
    }
    return build_controller_snapshot(
        records=records,
        covered_resource_types=tuple(enumeration.covered_resource_types),
        name_hints=hints,
    )


def _active_source_digest(module: ModuleIndex) -> str:
    return digest_active_source(
        (source.relative_path, source.source)
        for source in module.sources
        if source.active
    )


def _plan_time_live_digest(snapshot: ReconcileSnapshot) -> str:
    return digest_controller_observations(
        (observation.address.absolute, observation.live)
        for observation in snapshot.resources
    )


def _check_is_blocked(plan: ReconcilePlan) -> bool:
    if plan.blocked or plan.edits:
        return True
    return any(
        decision.reason is ReasonCode.COMPUTED_OR_UNKNOWN
        or decision.disposition in {
            Disposition.CAPTURE_LIVE,
            Disposition.APPEND,
            Disposition.REMOVE,
        }
        for decision in plan.decisions
    )


def _decision_item(reason: ReasonCode, address: str) -> OutcomeItem:
    return _profile_item(reason.value, address)


def _profile_item(reason: str, address: str | None = None) -> OutcomeItem:
    severity, message = COMMAND_PROFILES["check"].items[reason]
    reference = None if address is None else opaque_reference(address)
    return OutcomeItem(reason, severity, reference, message)


def _thaw_object(value: FrozenObject) -> dict[str, object]:
    return {key: _thaw(item) for key, item in value.items}


def _thaw(value: FrozenValue) -> object:
    if isinstance(value, FrozenObject):
        return _thaw_object(value)
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return value
