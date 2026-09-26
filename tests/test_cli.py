# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 James Braid
"""Public CLI contract for the 0.10 cutover."""

from __future__ import annotations

from pathlib import Path

import pytest

import ubitofu.cli as cli
from ubitofu.errors import ControllerResponseError
from ubitofu.outcomes import CommandOutcome, OutcomeItem
from ubitofu.values import freeze_value


def _config(tmp_path: Path) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(
        'controller_url = "https://unifi.example"\n'
        'site = "default"\n'
        'api_key_source = "env"\n'
        'api_key_ref = "UNIFI_API_KEY"\n'
        f'workdir = "{tmp_path}"\n'
    )
    return path


def _outcome(command: str, *, blocked: bool = False) -> CommandOutcome:
    profiles = {
        "generate": (
            "generation preview complete",
            (("active_source", "a" * 64), ("controller", "b" * 64), ("provider_schema", "c" * 64)),
            freeze_value({"changed_paths": [], "candidate_digests": []}),
        ),
        "reconcile": (
            "reconciliation complete",
            (("active_source", "a" * 64), ("controller", "b" * 64)),
            freeze_value({"changed_paths": [], "candidate_digests": []}),
        ),
        "check": (
            "saved plan check complete",
            (
                ("saved_plan", "a" * 64),
                ("active_source", "b" * 64),
                ("plan_time_live", "c" * 64),
                ("fresh_controller", "d" * 64),
            ),
            None,
        ),
        "inspect": (
            "inspection complete",
            (("controller", "a" * 64), ("provider_schema", "b" * 64)),
            None,
        ),
        "health_snapshot": (
            "health snapshot complete",
            (("controller", "a" * 64),),
            freeze_value({"subsystems": []}),
        ),
        "health_compare": (
            "health comparison complete",
            (("health_before", "a" * 64), ("health_after", "b" * 64)),
            freeze_value({"subsystems": []}),
        ),
    }
    summary, digests, payload = profiles[command]
    items = (
        OutcomeItem("unsafe_plan", "blocking", None, "saved plan is unsafe"),
    ) if blocked else ()
    return CommandOutcome(command, False, blocked, summary, items, digests, payload)


def test_parser_exposes_exact_public_command_tree():
    parser = cli.build_parser()
    accepted = [
        ["generate", "--config", "c.toml"],
        ["reconcile", "--config", "c.toml"],
        ["reconcile", "--dry-run", "--config", "c.toml"],
        ["check", "--plan", "saved.tfplan", "--config", "c.toml"],
        ["inspect", "--config", "c.toml"],
        ["health", "snapshot", "--config", "c.toml"],
        ["health", "compare", "--before", "before.json", "--config", "c.toml"],
    ]
    for argv in accepted:
        parser.parse_args(argv)

    for removed in ("enumerate", "verify"):
        with pytest.raises(SystemExit):
            parser.parse_args([removed, "--config", "c.toml"])
    with pytest.raises(SystemExit):
        parser.parse_args(["reconcile", "--check", "--config", "c.toml"])


def test_common_output_options_are_consistent():
    parser = cli.build_parser()
    for argv in (
        ["generate"],
        ["reconcile", "--dry-run"],
        ["check", "--plan", "p"],
        ["inspect"],
        ["health", "snapshot"],
        ["health", "compare", "--before", "b"],
    ):
        parsed = parser.parse_args(
            [*argv, "--config", "c.toml", "--format", "json", "--output", "receipt.json"]
        )
        assert parsed.format == "json"
        assert parsed.output == "receipt.json"


@pytest.mark.parametrize(
    "legacy",
    ["--controller-url", "--site", "--api-key-source", "--mode"],
)
def test_parser_rejects_legacy_override_and_mode_flags(legacy):
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(
            ["generate", "--config", "c.toml", legacy, "legacy-value"]
        )


@pytest.mark.parametrize(
    "argv,function,command,expected",
    [
        (["generate"], "run_generate", "generate", {}),
        (["reconcile"], "run_reconcile", "reconcile", {"dry_run": False}),
        (["reconcile", "--dry-run"], "run_reconcile", "reconcile", {"dry_run": True}),
        (
            ["check", "--plan", "saved.tfplan"],
            "run_check",
            "check",
            {"plan_path": Path("saved.tfplan")},
        ),
        (["inspect"], "run_inspect", "inspect", {}),
        (["health", "snapshot"], "run_health_snapshot", "health_snapshot", {}),
        (
            ["health", "compare", "--before", "before.json"],
            "run_health_compare",
            "health_compare",
            {"before_path": Path("before.json")},
        ),
    ],
)
def test_main_dispatches_each_command_once(
    monkeypatch, tmp_path, capsys, argv, function, command, expected
):
    config = _config(tmp_path)
    calls = []

    def run(**kwargs):
        calls.append(kwargs)
        return _outcome(command)

    monkeypatch.setattr(cli.pipeline, function, run)
    rc = cli.main([*argv, "--config", str(config)])

    assert rc == 0
    assert len(calls) == 1
    for key, value in expected.items():
        assert calls[0][key] == value
    assert capsys.readouterr().out


def test_blocking_outcome_maps_to_exit_three(monkeypatch, tmp_path):
    monkeypatch.setattr(
        cli.pipeline,
        "run_check",
        lambda **kwargs: _outcome("check", blocked=True),
    )
    assert cli.main(
        ["check", "--plan", "saved.tfplan", "--config", str(_config(tmp_path))]
    ) == 3


def test_config_error_maps_to_exit_two(tmp_path, capsys):
    bad = tmp_path / "bad.toml"
    bad.write_text('controller_url = "https://unifi.example"\nsite = "default"\n')
    assert cli.main(["inspect", "--config", str(bad)]) == 2
    assert "config error" in capsys.readouterr().err


def test_missing_api_key_environment_variable_is_bounded_config_error(
    monkeypatch, tmp_path, capsys
):
    reference = "SYNTHETIC_MISSING_API_KEY_REFERENCE"
    monkeypatch.delenv(reference, raising=False)
    config = tmp_path / "config.toml"
    config.write_text(
        'controller_url = "https://unifi.example"\n'
        'site = "default"\n'
        'api_key_source = "env"\n'
        f'api_key_ref = "{reference}"\n'
        f'workdir = "{tmp_path}"\n'
    )

    assert cli.main(["health", "snapshot", "--config", str(config)]) == 2
    error = capsys.readouterr().err
    assert "API key environment variable is unset" in error
    assert reference not in error


def test_missing_password_environment_variable_is_bounded_config_error(
    monkeypatch, tmp_path, capsys
):
    reference = "SYNTHETIC_MISSING_PASSWORD_REFERENCE"
    monkeypatch.delenv(reference, raising=False)
    config = tmp_path / "config.toml"
    config.write_text(
        'controller_url = "https://unifi.example"\n'
        'site = "default"\n'
        'dialect = "classic"\n'
        'username = "admin"\n'
        'password_source = "env"\n'
        f'password_ref = "{reference}"\n'
        f'workdir = "{tmp_path}"\n'
    )

    assert cli.main(["health", "snapshot", "--config", str(config)]) == 2
    error = capsys.readouterr().err
    assert "password environment variable is unset" in error
    assert reference not in error


def test_invalid_custom_ca_content_is_bounded_config_error(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("SYNTHETIC_API_KEY", "synthetic-key-value")
    bundle = tmp_path / "controller-ca.pem"
    bundle.write_text("synthetic invalid PEM content")
    config = tmp_path / "config.toml"
    config.write_text(
        'controller_url = "https://unifi.example"\n'
        'site = "default"\n'
        'api_key_source = "env"\n'
        'api_key_ref = "SYNTHETIC_API_KEY"\n'
        f'ca_bundle = "{bundle}"\n'
        f'workdir = "{tmp_path}"\n'
    )

    assert cli.main(["health", "snapshot", "--config", str(config)]) == 2
    error = capsys.readouterr().err
    assert "custom CA bundle is invalid" in error
    assert str(bundle) not in error
    assert "synthetic invalid PEM content" not in error


def test_cli_binds_receipt_owner_to_configured_workdir(monkeypatch, tmp_path):
    config = _config(tmp_path)
    monkeypatch.setattr(cli.pipeline, "run_inspect", lambda **kwargs: _outcome("inspect"))
    captured = {}

    def emit(*args, **kwargs):
        captured.update(kwargs)

    monkeypatch.setattr(cli, "emit_output", emit)

    assert cli.main(["inspect", "--config", str(config)]) == 0
    assert captured["owner_root"] == tmp_path


def test_operational_error_maps_to_one_without_leaking_details(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(
        cli.pipeline,
        "run_reconcile",
        lambda **kwargs: (_ for _ in ()).throw(RuntimeError("secret controller detail")),
    )
    assert cli.main(["reconcile", "--config", str(_config(tmp_path))]) == 1
    error = capsys.readouterr().err
    assert "unexpected internal error" in error
    assert "secret controller detail" not in error


@pytest.mark.parametrize(
    "error,expected",
    [
        (ControllerResponseError("login", None, "controller request failed"), "cannot reach"),
        (ControllerResponseError("login", 401, "authentication failed"), "authentication failed"),
    ],
)
def test_typed_controller_failures_keep_stable_operator_messages(
    monkeypatch, tmp_path, capsys, error, expected
):
    monkeypatch.setattr(
        cli.pipeline,
        "run_inspect",
        lambda **kwargs: (_ for _ in ()).throw(error),
    )

    assert cli.main(["inspect", "--config", str(_config(tmp_path))]) == 1
    assert expected in capsys.readouterr().err


def test_output_file_uses_the_same_outcome_and_format(monkeypatch, tmp_path):
    outcome = _outcome("inspect")
    monkeypatch.setattr(cli.pipeline, "run_inspect", lambda **kwargs: outcome)
    output = tmp_path / "receipt.json"

    assert cli.main(
        [
            "inspect",
            "--config",
            str(_config(tmp_path)),
            "--format",
            "json",
            "--output",
            str(output),
        ]
    ) == 0
    assert output.stat().st_mode & 0o777 == 0o600
    assert b'"command":"inspect"' in output.read_bytes()

    output.write_text("old receipt")
    output.chmod(0o644)
    assert cli.main(
        [
            "inspect",
            "--config",
            str(_config(tmp_path)),
            "--format",
            "json",
            "--output",
            str(output),
        ]
    ) == 0
    assert output.stat().st_mode & 0o777 == 0o600
    assert b'"command":"inspect"' in output.read_bytes()


def test_reporting_failure_maps_to_operational_exit(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(cli.pipeline, "run_inspect", lambda **kwargs: _outcome("inspect"))
    monkeypatch.setattr(
        cli,
        "emit_output",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            cli.UbitofuError("receipt output write failed")
        ),
    )

    assert cli.main(["inspect", "--config", str(_config(tmp_path))]) == 1
    assert "unexpected internal error" in capsys.readouterr().err


@pytest.mark.parametrize(
    "command,output_name",
    [
        (["reconcile", "--dry-run"], "main.tf"),
        (["reconcile"], "nested/main.tofu"),
        (["generate"], "generated.tf.json"),
        (["generate"], "COVERAGE.md"),
        (["inspect"], "ubitofu-imports.tf"),
        (["inspect"], ".ubitofu/receipt.json"),
    ],
)
def test_output_cannot_collide_with_source_candidate_or_control_paths(
    monkeypatch, tmp_path, command, output_name
):
    invoked = False

    def fail_if_called(**kwargs):
        nonlocal invoked
        invoked = True
        raise AssertionError("pipeline ran before output collision was rejected")

    function = "run_reconcile" if command[0] == "reconcile" else f"run_{command[0]}"
    monkeypatch.setattr(cli.pipeline, function, fail_if_called)
    output = tmp_path / output_name

    assert cli.main(
        [*command, "--config", str(_config(tmp_path)), "--output", str(output)]
    ) == 2
    assert invoked is False
    assert not output.exists()


@pytest.mark.parametrize(
    "command,input_flag,input_name",
    [
        ("check", "--plan", "saved.tfplan"),
        ("health compare", "--before", "before.json"),
    ],
)
def test_output_cannot_replace_command_input(tmp_path, command, input_flag, input_name):
    source = tmp_path.parent / f"{tmp_path.name}-{input_name}"
    source.write_bytes(b"input bytes\n")
    argv = command.split()
    assert cli.main(
        [
            *argv,
            input_flag,
            str(source),
            "--config",
            str(_config(tmp_path)),
            "--output",
            str(source),
        ]
    ) == 2
    assert source.read_bytes() == b"input bytes\n"


def test_relative_saved_plan_collision_uses_workdir_semantics(monkeypatch, tmp_path):
    workdir = tmp_path / "module"
    elsewhere = tmp_path / "elsewhere"
    workdir.mkdir()
    elsewhere.mkdir()
    config = _config(workdir)
    plan = workdir / "saved.tfplan"
    plan.write_bytes(b"saved plan\n")
    monkeypatch.chdir(elsewhere)

    assert cli.main(
        [
            "check",
            "--plan",
            "saved.tfplan",
            "--config",
            str(config),
            "--output",
            str(plan),
        ]
    ) == 2
    assert plan.read_bytes() == b"saved plan\n"


def test_version_is_0_10_1():
    import ubitofu

    assert ubitofu.__version__ == "0.10.2"
