# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 James Braid
import pytest

from ubitofu.cli import build_parser, main
from ubitofu.config import Config, load_config, resolve_api_key


def test_load_config(fixtures_dir):
    cfg = load_config(str(fixtures_dir / "config.toml"))
    assert cfg.controller_url == "https://unifi.example"
    assert cfg.site == "default"
    assert cfg.api_key_source == "env"
    assert cfg.op_vault == "ExampleVault"


def test_op_vault_is_required(tmp_path):
    # No baked-in default vault: op_vault must come from the operator's config.
    p = tmp_path / "config.toml"
    p.write_text(
        'controller_url = "https://unifi.example"\n'
        'site = "default"\n'
        'api_key_source = "env"\n'
        'api_key_ref = "UNIFI_API_KEY"\n'
    )
    with pytest.raises(TypeError):
        load_config(str(p))


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

    def fake_reconcile(cfg, out):
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


def test_main_enumerate_prints_gaps(monkeypatch, fixtures_dir, capsys):
    import ubitofu.cli as climod

    def fake_enumerate(cfg, mode, out):
        print("Coverage gaps:\n  - 2 objects at v2/.../nat", file=out)
        return 0

    monkeypatch.setattr(climod, "cmd_enumerate", fake_enumerate)
    rc = main(["enumerate", "--config", str(fixtures_dir / "config.toml")])
    assert rc == 0
    assert "Coverage gaps" in capsys.readouterr().out
