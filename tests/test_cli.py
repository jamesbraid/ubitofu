# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 James Braid
from pathlib import Path

import pytest

from ubitofu.cli import build_parser, main
from ubitofu.config import Config, load_config, resolve_api_key


def test_load_config(fixtures_dir):
    cfg = load_config(str(fixtures_dir / "config.toml"))
    assert cfg.controller_url == "https://unifi.example"
    assert cfg.site == "default"
    assert cfg.api_key_source == "env"
    assert cfg.op_vault == "ExampleVault"


def test_op_vault_defaults_to_empty(tmp_path):
    # op_vault now has a default empty string; classic configs can omit it.
    p = tmp_path / "config.toml"
    p.write_text(
        'controller_url = "https://unifi.example"\n'
        'site = "default"\n'
        'api_key_source = "env"\n'
        'api_key_ref = "UNIFI_API_KEY"\n'
    )
    cfg = load_config(str(p))
    assert cfg.op_vault == ""


def test_relative_workdir_resolved_to_absolute(tmp_path, monkeypatch):
    # TofuRunner runs tofu with cwd=workdir while the pipelines pass
    # workdir-prefixed output paths on the command line (-out=<workdir>/tf.plan).
    # With a relative workdir both cannot hold: tofu resolves the path from
    # inside the workdir, so "./work" becomes work/work/tf.plan.
    p = tmp_path / "config.toml"
    p.write_text(
        'controller_url = "https://unifi.example"\n'
        'site = "default"\n'
        'api_key_source = "env"\n'
        'api_key_ref = "UNIFI_API_KEY"\n'
        'op_vault = "ExampleVault"\n'
        'workdir = "./work"\n'
    )
    monkeypatch.chdir(tmp_path)
    cfg = load_config(str(p))
    assert Path(cfg.workdir).is_absolute()
    assert Path(cfg.workdir) == (tmp_path / "work").resolve()


def test_default_workdir_resolved_to_absolute(tmp_path, monkeypatch):
    # Direct construction (library use, tests) must uphold the same invariant.
    monkeypatch.chdir(tmp_path)
    cfg = Config("https://x", "default", "env", "UNIFI_API_KEY", "ExampleVault")
    assert Path(cfg.workdir).is_absolute()
    assert Path(cfg.workdir) == tmp_path.resolve()


def test_resolve_api_key_from_env(fixtures_dir):
    cfg = load_config(str(fixtures_dir / "config.toml"))
    assert resolve_api_key(cfg, environ={"UNIFI_API_KEY": "SEKRET"}) == "SEKRET"


def test_resolve_api_key_from_op_uses_reader():
    cfg = Config("https://x", "default", "op", "op://ExampleVault/unifi/key", "ExampleVault")
    assert resolve_api_key(cfg, environ={}, op_reader=lambda ref: "OPKEY") == "OPKEY"


def test_parser_has_four_subcommands():
    parser = build_parser()
    # smoke: parsing each subcommand does not error
    for cmd in ("enumerate", "generate", "reconcile", "verify"):
        ns = parser.parse_args([cmd, "--config", "c.toml"])
        assert ns.command == cmd


def test_reconcile_config_is_set():
    parser = build_parser()
    ns = parser.parse_args(["reconcile", "--config", "c.toml"])
    assert ns.command == "reconcile"
    assert ns.config == "c.toml"


def test_main_dispatches_reconcile(monkeypatch, fixtures_dir):
    import ubitofu.cli as climod

    called = {}

    def fake_reconcile(cfg, out, check=False):
        called["dispatched"] = True
        print("Reconcile: already in sync — no changes.", file=out)
        return 0

    monkeypatch.setattr(climod, "cmd_reconcile", fake_reconcile)
    rc = main(["reconcile", "--config", str(fixtures_dir / "config.toml")])
    assert rc == 0
    assert called["dispatched"] is True


def test_no_apply_flag_anywhere(capsys):
    # Global Constraint #1: no path exposes apply.
    parser = build_parser()
    help_text = parser.format_help()
    assert "apply" not in help_text.lower()


def test_reconcile_subcommand_rejects_mode():
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["reconcile", "--config", "x", "--mode", "bulk"])


def test_generate_still_accepts_mode():
    parser = build_parser()
    ns = parser.parse_args(["generate", "--config", "x", "--mode", "incremental"])
    assert ns.mode == "incremental"


# --- Error boundary tests ---


def test_main_maps_controller_unreachable_to_one_line(monkeypatch, capsys, fixtures_dir):
    import httpx

    import ubitofu.cli as climod

    def boom(*a, **k):
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(climod, "cmd_reconcile", boom)
    rc = main(["reconcile", "--config", str(fixtures_dir / "config.toml")])
    err = capsys.readouterr().err
    assert rc != 0
    assert "ubitofu:" in err
    assert "traceback" not in err.lower()
    assert "controller" in err.lower() or "unreachable" in err.lower()


def test_main_maps_tofu_failure_to_one_line(monkeypatch, capsys, fixtures_dir):
    import ubitofu.cli as climod
    from ubitofu.tofu_runner import TofuError

    def boom(*a, **k):
        raise TofuError("plan failed: credentials expired")

    monkeypatch.setattr(climod, "cmd_reconcile", boom)
    rc = main(["reconcile", "--config", str(fixtures_dir / "config.toml")])
    err = capsys.readouterr().err
    assert rc != 0
    assert "ubitofu:" in err
    assert "tofu" in err.lower()
    assert "traceback" not in err.lower()


def test_main_maps_op_auth_failure_to_one_line(monkeypatch, capsys, fixtures_dir):
    import subprocess

    import ubitofu.cli as climod

    def boom(*a, **k):
        raise subprocess.CalledProcessError(1, "op")

    monkeypatch.setattr(climod, "cmd_reconcile", boom)
    rc = main(["reconcile", "--config", str(fixtures_dir / "config.toml")])
    err = capsys.readouterr().err
    assert rc != 0
    assert "ubitofu:" in err
    assert "1password" in err.lower() or "op signin" in err.lower()
    assert "traceback" not in err.lower()


def test_main_unexpected_error_surfaces_type_and_message(monkeypatch, capsys, fixtures_dir):
    import ubitofu.cli as climod

    def boom(*a, **k):
        raise RuntimeError("something exploded unexpectedly")

    monkeypatch.setattr(climod, "cmd_reconcile", boom)
    rc = main(["reconcile", "--config", str(fixtures_dir / "config.toml")])
    err = capsys.readouterr().err
    assert rc != 0
    assert "ubitofu:" in err
    assert "RuntimeError" in err
    assert "something exploded unexpectedly" in err
    assert "please report" in err


def test_main_enumerate_prints_gaps(monkeypatch, fixtures_dir, capsys):
    import ubitofu.cli as climod

    def fake_enumerate(cfg, mode, out):
        print("Coverage gaps:\n  - 2 objects at v2/.../nat", file=out)
        return 0

    monkeypatch.setattr(climod, "cmd_enumerate", fake_enumerate)
    rc = main(["enumerate", "--config", str(fixtures_dir / "config.toml")])
    assert rc == 0
    assert "Coverage gaps" in capsys.readouterr().out


def test_version_is_exposed():
    import ubitofu
    assert ubitofu.__version__  # non-empty
    assert ubitofu.__version__[0].isdigit()


def test_python_dash_m_entrypoint_runs():
    import subprocess
    import sys
    r = subprocess.run([sys.executable, "-m", "ubitofu", "--help"],
                       capture_output=True, text=True)
    assert r.returncode == 0
    assert "reconcile" in r.stdout


def test_enumerate_errors_actionably_without_init(monkeypatch, fixtures_dir, capsys):
    import ubitofu.cli as climod
    from ubitofu.tofu_runner import TofuError

    class DummyController:
        # Item 2: cmd_enumerate closes the controller in a finally block —
        # the stand-in must carry a close() like the real Controller does.
        def close(self):
            pass

    monkeypatch.setattr(climod, "_controller", lambda cfg: DummyController())

    class FailingRunner:
        def __init__(self, workdir):
            pass

        def providers_schema(self):
            raise TofuError("no schema available")

    monkeypatch.setattr(climod, "TofuRunner", FailingRunner)
    rc = main(["enumerate", "--config", str(fixtures_dir / "config.toml")])
    assert rc == 1
    err = capsys.readouterr().err
    assert "tofu init" in err  # actionable: no degraded silent mode


def test_cmd_enumerate_closes_the_controller(monkeypatch, fixtures_dir):
    import io

    import ubitofu.cli as climod
    from ubitofu.cli import cmd_enumerate
    from ubitofu.config import load_config
    from ubitofu.enumerator import EnumerationResult

    class RecordingController:
        closed = False

        def collection(self, endpoint):
            return []

        def close(self):
            self.closed = True

    sentinel = RecordingController()
    monkeypatch.setattr(climod, "_controller", lambda cfg: sentinel)

    # The coverage audit refuses to run blind without unifi_setting present
    # in the schema (coverage.setting_schema_sections) — carry the same
    # minimal stub other tests use so cmd_enumerate's audit no-ops cleanly.
    schema = {"provider_schemas": {"registry.terraform.io/jamesbraid/unifi": {
        "resource_schemas": {"unifi_setting": {"block": {"attributes": {}}}}}}}

    class FakeRunner:
        def __init__(self, workdir):
            pass

        def providers_schema(self):
            return schema

    monkeypatch.setattr(climod, "TofuRunner", FakeRunner)
    monkeypatch.setattr(climod, "enumerate_controller", lambda ctl: EnumerationResult())

    cfg = load_config(str(fixtures_dir / "config.toml"))
    cmd_enumerate(cfg, "bulk", io.StringIO())

    assert sentinel.closed is True


def test_reconcile_help_documents_exit_codes(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["reconcile", "--help"])
    assert exc.value.code == 0
    text = capsys.readouterr().out
    assert "exit codes" in text.lower()
    for token in ("10", "11", "12", "13"):
        assert token in text, token


def test_verify_help_documents_exit_codes(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["verify", "--help"])
    assert exc.value.code == 0
    assert "exit codes" in capsys.readouterr().out.lower()


def test_main_config_error_exits_2_with_message(tmp_path, capsys):
    from ubitofu.config import ConfigError

    p = tmp_path / "bad.toml"
    p.write_text(
        'controller_url = "https://c"\n'
        'site = "default"\n'
        'dialect = "classic"\n'
    )
    rc = main(["reconcile", "--config", str(p)])
    err = capsys.readouterr().err
    assert rc == 2
    assert "ubitofu: config error:" in err
    assert "classic" in err
    # Sanity: this really is the exception load_config raises, not some
    # other path swallowing a different error type into the same message.
    with pytest.raises(ConfigError):
        from ubitofu.config import load_config
        load_config(str(p))


def test_flag_rescues_config_missing_api_key_source(monkeypatch, tmp_path, capsys):
    # Regression (direction 1): validation used to run before the CLI flag
    # overrides, so a config file missing api_key_source died on
    # ConfigError before --api-key-source env ever got a chance to fill
    # the gap. Assert we now get all the way past validation into command
    # execution — proven by a sentinel raised from controller_from_config,
    # the first thing the reconcile pipeline calls.
    import ubitofu.pipeline as pipelinemod

    p = tmp_path / "config.toml"
    p.write_text(
        'controller_url = "https://unifi.example"\n'
        'site = "default"\n'
        'api_key_ref = "UNIFI_API_KEY"\n'
        'op_vault = "ExampleVault"\n'
    )
    monkeypatch.setenv("UNIFI_API_KEY", "k")

    class _Sentinel(Exception):
        pass

    def boom(cfg):
        raise _Sentinel("reached-command-execution")

    monkeypatch.setattr(pipelinemod, "controller_from_config", boom)
    rc = main(["reconcile", "--config", str(p), "--api-key-source", "env"])
    err = capsys.readouterr().err
    assert "config error" not in err
    assert rc == 1
    assert "_Sentinel" in err
    assert "reached-command-execution" in err


def test_flag_can_invalidate_a_valid_config(tmp_path, capsys):
    # Regression (direction 2): the same flag can also break a config that
    # was valid on disk. --api-key-source op with no op_vault must still
    # be caught, not bypass validation because it arrived as a flag.
    p = tmp_path / "config.toml"
    p.write_text(
        'controller_url = "https://unifi.example"\n'
        'site = "default"\n'
        'api_key_source = "env"\n'
        'api_key_ref = "UNIFI_API_KEY"\n'
    )
    rc = main(["reconcile", "--config", str(p), "--api-key-source", "op"])
    err = capsys.readouterr().err
    assert rc == 2
    assert "ubitofu: config error:" in err
    assert "op_vault" in err


def test_main_maps_401_to_authentication_failure(monkeypatch, capsys, fixtures_dir):
    import httpx

    import ubitofu.cli as climod

    def boom(*a, **k):
        request = httpx.Request("GET", "https://unifi.example/api/s/default/x")
        response = httpx.Response(401, request=request)
        raise httpx.HTTPStatusError("401", request=request, response=response)

    monkeypatch.setattr(climod, "cmd_reconcile", boom)
    rc = main(["reconcile", "--config", str(fixtures_dir / "config.toml")])
    err = capsys.readouterr().err
    assert rc == 1
    assert "authentication failed" in err.lower()
    assert "unifi.example" in err
    assert "cannot reach" not in err.lower()


def test_main_maps_403_to_authentication_failure(monkeypatch, capsys, fixtures_dir):
    import httpx

    import ubitofu.cli as climod

    def boom(*a, **k):
        request = httpx.Request("GET", "https://unifi.example/api/s/default/x")
        response = httpx.Response(403, request=request)
        raise httpx.HTTPStatusError("403", request=request, response=response)

    monkeypatch.setattr(climod, "cmd_reconcile", boom)
    rc = main(["reconcile", "--config", str(fixtures_dir / "config.toml")])
    err = capsys.readouterr().err
    assert rc == 1
    assert "authentication failed" in err.lower()


def test_main_maps_non_auth_http_status_error_to_cannot_reach(monkeypatch, capsys, fixtures_dir):
    import httpx

    import ubitofu.cli as climod

    def boom(*a, **k):
        request = httpx.Request("GET", "https://unifi.example/api/s/default/x")
        response = httpx.Response(500, request=request)
        raise httpx.HTTPStatusError("500", request=request, response=response)

    monkeypatch.setattr(climod, "cmd_reconcile", boom)
    rc = main(["reconcile", "--config", str(fixtures_dir / "config.toml")])
    err = capsys.readouterr().err
    assert rc == 1
    assert "cannot reach" in err.lower()
    assert "authentication failed" not in err.lower()


def test_main_maps_connect_error_still_cannot_reach(monkeypatch, capsys, fixtures_dir):
    # Regression guard: a plain transport error (not an HTTPStatusError)
    # must keep hitting the generic httpx.HTTPError arm, unaffected by the
    # new HTTPStatusError-specific auth handling.
    import httpx

    import ubitofu.cli as climod

    def boom(*a, **k):
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(climod, "cmd_reconcile", boom)
    rc = main(["reconcile", "--config", str(fixtures_dir / "config.toml")])
    err = capsys.readouterr().err
    assert rc == 1
    assert "cannot reach" in err.lower()


def test_exit_epilog_documents_usage_error_for_config(capsys):
    with pytest.raises(SystemExit):
        main(["reconcile", "--help"])
    text = capsys.readouterr().out
    assert "usage error" in text.lower()
    assert "config" in text.lower()


def test_reconcile_check_flag_wired(monkeypatch, fixtures_dir, capsys):
    seen = {}

    def fake_run(cfg, out, check=False):
        seen["check"] = check
        return 0

    monkeypatch.setattr("ubitofu.pipeline.run_reconcile", fake_run)
    monkeypatch.setenv("UNIFI_API_KEY", "k")
    rc = main(["reconcile", "--check", "--config", str(fixtures_dir / "config.toml")])
    assert rc == 0
    assert seen["check"] is True
