# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 James Braid
"""Provider-admission composition tests for public command pipelines."""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

import ubitofu.pipeline as pipeline
from ubitofu.config import Config
from ubitofu.errors import ProviderContractError


def _cfg(tmp_path: Path, *, contract: bool) -> Config:
    bundle = {
        "provider_contract": "contract.json",
        "provider_contract_checksum": "contract.json.sha256",
        "provider_binary": "provider",
        "provider_schema_cli": "tofu",
    } if contract else {}
    return Config(
        "https://unifi.example",
        "default",
        "env",
        "SYNTHETIC_API_KEY",
        workdir=str(tmp_path),
        **bundle,
    )


class _Controller:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


def _session(workdir: Path) -> SimpleNamespace:
    return SimpleNamespace(workdir=workdir, plan_path=workdir / "private.tfplan")


@pytest.mark.parametrize("contract", [False, True])
@pytest.mark.parametrize("command", ["generate", "reconcile", "check", "inspect"])
def test_provider_execution_is_the_single_command_runner_boundary(
    monkeypatch, tmp_path, contract, command
):
    cfg = _cfg(tmp_path, contract=contract)
    events: list[str] = []
    controller = _Controller()
    cached_schema = {"provider_schemas": {"synthetic": {}}}
    runner = SimpleNamespace(cached_provider_schema=cached_schema)
    received: dict[str, object] = {}
    expected = object()

    @contextmanager
    def fake_session(workdir, **kwargs):
        events.append("lock")
        yield _session(workdir)

    @contextmanager
    def fake_execution(*, cfg, workdir):
        events.append("admit")
        received["cfg"] = cfg
        received["admission_workdir"] = workdir
        yield SimpleNamespace(
            schema=cached_schema,
            runner=lambda *, workdir, plan_path=None: received.update(
                runner_workdir=workdir,
                plan_path=plan_path,
            ) or runner,
        )

    def make_controller(value):
        events.append("controller")
        assert value is cfg
        return controller

    monkeypatch.setattr(pipeline, "runtime_session", fake_session)
    monkeypatch.setattr(pipeline, "provider_execution", fake_execution)
    monkeypatch.setattr(pipeline, "controller_from_config", make_controller)

    if command == "generate":
        monkeypatch.setattr(
            pipeline,
            "prepare_generate",
            lambda **kwargs: received.update(generate_runner=kwargs["runner"])
            or SimpleNamespace(blocked=True),
        )
        monkeypatch.setattr(pipeline, "generate_outcome", lambda preview: expected)
        result = pipeline.run_generate(cfg=cfg)
    elif command == "reconcile":
        monkeypatch.setattr(
            pipeline,
            "prepare_reconcile",
            lambda **kwargs: received.update(reconcile_runner=kwargs["runner"])
            or SimpleNamespace(valid=True, plan=SimpleNamespace(blocked=True), files=()),
        )
        monkeypatch.setattr(pipeline, "reconcile_outcome", lambda preview: expected)
        result = pipeline.run_reconcile(cfg=cfg, dry_run=True)
    elif command == "check":
        monkeypatch.setattr(
            pipeline,
            "check_saved_plan",
            lambda **kwargs: received.update(check_runner=kwargs["runner"])
            or expected,
        )
        result = pipeline.run_check(cfg=cfg, plan_path=tmp_path / "saved.tfplan")
    else:
        monkeypatch.setattr(
            pipeline,
            "inspect_coverage",
            lambda **kwargs: received.update(inspect_runner=kwargs["runner"])
            or expected,
        )
        result = pipeline.run_inspect(cfg=cfg)

    assert result is expected
    assert received["cfg"] is cfg
    assert received["admission_workdir"] == tmp_path
    assert received["runner_workdir"] == tmp_path
    expected_plan_path = tmp_path / "private.tfplan" if command == "reconcile" else None
    assert received["plan_path"] == expected_plan_path
    assert runner.cached_provider_schema is cached_schema
    assert received[f"{command}_runner"] is runner
    assert events.index("admit") < events.index("controller")
    if command == "inspect":
        assert events == ["admit", "controller"]
    else:
        assert events.index("lock") < events.index("admit")
    assert controller.closed is True


@pytest.mark.parametrize("command", ["generate", "reconcile", "check", "inspect"])
def test_provider_admission_failure_stops_controller_construction(monkeypatch, tmp_path, command):
    cfg = _cfg(tmp_path, contract=True)

    @contextmanager
    def fake_session(workdir, **kwargs):
        yield _session(workdir)

    @contextmanager
    def rejected_execution(**kwargs):
        raise ProviderContractError("untrusted evidence")
        yield

    monkeypatch.setattr(pipeline, "runtime_session", fake_session)
    monkeypatch.setattr(pipeline, "provider_execution", rejected_execution)
    monkeypatch.setattr(
        pipeline,
        "controller_from_config",
        lambda cfg: pytest.fail("controller was created after failed provider admission"),
    )

    with pytest.raises(ProviderContractError):
        if command == "generate":
            pipeline.run_generate(cfg=cfg)
        elif command == "reconcile":
            pipeline.run_reconcile(cfg=cfg, dry_run=True)
        elif command == "check":
            pipeline.run_check(cfg=cfg, plan_path=tmp_path / "saved.tfplan")
        else:
            pipeline.run_inspect(cfg=cfg)


def test_provider_execution_preserves_reconcile_dry_run_no_write(monkeypatch, tmp_path):
    cfg = _cfg(tmp_path, contract=True)
    destination = tmp_path / "main.tf"
    destination.write_bytes(b"before\n")

    @contextmanager
    def fake_session(workdir, **kwargs):
        yield _session(workdir)

    @contextmanager
    def fake_execution(**kwargs):
        yield SimpleNamespace(runner=lambda **kwargs: object())

    monkeypatch.setattr(pipeline, "runtime_session", fake_session)
    monkeypatch.setattr(pipeline, "provider_execution", fake_execution)
    monkeypatch.setattr(pipeline, "controller_from_config", lambda cfg: _Controller())
    monkeypatch.setattr(
        pipeline,
        "prepare_reconcile",
        lambda **kwargs: SimpleNamespace(valid=True, plan=SimpleNamespace(blocked=False), files=()),
    )
    monkeypatch.setattr(pipeline, "reconcile_outcome", lambda preview: object())
    monkeypatch.setattr(
        pipeline,
        "prepare_transaction",
        lambda **kwargs: pytest.fail("dry-run created a transaction"),
    )

    pipeline.run_reconcile(cfg=cfg, dry_run=True)

    assert destination.read_bytes() == b"before\n"


@pytest.mark.parametrize("command", ["snapshot", "compare"])
def test_health_commands_bypass_provider_admission(monkeypatch, tmp_path, command):
    cfg = _cfg(tmp_path, contract=True)
    controller = _Controller()
    expected = object()

    monkeypatch.setattr(
        pipeline,
        "provider_execution",
        lambda **kwargs: pytest.fail("health entered provider admission"),
    )
    monkeypatch.setattr(pipeline, "controller_from_config", lambda cfg: controller)
    monkeypatch.setattr(pipeline, "capture_health", lambda controller: object())
    if command == "snapshot":
        monkeypatch.setattr(pipeline, "health_snapshot_outcome", lambda snapshot: expected)
        result = pipeline.run_health_snapshot(cfg=cfg)
    else:
        monkeypatch.setattr(pipeline, "read_receipt_file", lambda *args, **kwargs: object())
        monkeypatch.setattr(pipeline, "health_snapshot_from_receipt", lambda value: object())
        monkeypatch.setattr(pipeline, "compare_health", lambda before, current: expected)
        result = pipeline.run_health_compare(cfg=cfg, before_path=tmp_path / "before.json")

    assert result is expected
    assert controller.closed is True
