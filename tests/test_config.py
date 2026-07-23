# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 James Braid
import pytest

from ubitofu.config import Config, load_config, resolve_password


def _cfg(**kw):
    base = dict(controller_url="https://c:8443", site="default")
    base.update(kw)
    return Config(**base)


def test_dialect_defaults_to_unifi_os():
    assert _cfg().dialect == "unifi-os"


def test_classic_fields_load_from_toml(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text(
        'controller_url = "https://c:8443"\n'
        'site = "default"\n'
        'dialect = "classic"\n'
        'username = "admin"\n'
        'password_source = "env"\n'
        'password_ref = "UNIFI_TEST_PASSWORD"\n'
    )
    cfg = load_config(str(p))
    assert (cfg.dialect, cfg.username) == ("classic", "admin")
    assert (cfg.password_source, cfg.password_ref) == ("env", "UNIFI_TEST_PASSWORD")


def test_resolve_password_env():
    cfg = _cfg(password_source="env", password_ref="PW")
    assert resolve_password(cfg, environ={"PW": "s3cret"}) == "s3cret"


def test_resolve_password_op():
    cfg = _cfg(password_source="op", password_ref="op://v/i/f")
    assert resolve_password(cfg, environ={}, op_reader=lambda ref: f"read:{ref}") == "read:op://v/i/f"


def test_resolve_password_unknown_source_raises():
    with pytest.raises(ValueError, match="password_source"):
        resolve_password(_cfg(), environ={})
