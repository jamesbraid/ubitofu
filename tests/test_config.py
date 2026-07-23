# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 James Braid
import pytest

from ubitofu.config import Config, ConfigError, load_config, resolve_password


def _cfg(**kw):
    base = dict(controller_url="https://c:8443", site="default")
    base.update(kw)
    return Config(**base)


def _write(tmp_path, text):
    p = tmp_path / "config.toml"
    p.write_text(text)
    return p


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


# ---------------------------------------------------------------------------
# load_config cross-field validation (ConfigError) — Item 1a
# ---------------------------------------------------------------------------


def test_load_config_valid_classic_passes(tmp_path):
    p = _write(
        tmp_path,
        'controller_url = "https://c:8443"\n'
        'site = "default"\n'
        'dialect = "classic"\n'
        'username = "admin"\n'
        'password_source = "env"\n'
        'password_ref = "UNIFI_TEST_PASSWORD"\n',
    )
    cfg = load_config(str(p))
    assert cfg.dialect == "classic"


def test_load_config_valid_unifi_os_passes(tmp_path):
    p = _write(
        tmp_path,
        'controller_url = "https://unifi.example"\n'
        'site = "default"\n'
        'api_key_source = "env"\n'
        'api_key_ref = "UNIFI_API_KEY"\n',
    )
    cfg = load_config(str(p))
    assert cfg.dialect == "unifi-os"


def test_load_config_rejects_unknown_dialect(tmp_path):
    p = _write(
        tmp_path,
        'controller_url = "https://c"\n'
        'site = "default"\n'
        'dialect = "udm"\n',
    )
    with pytest.raises(ConfigError, match="udm"):
        load_config(str(p))


def test_load_config_classic_missing_password_source_names_it(tmp_path):
    # The headline bug: dialect="classic" with password_source omitted must
    # fail loudly at load time, naming the missing dialect-specific keys —
    # never a bare runtime ValueError from resolve_password.
    p = _write(
        tmp_path,
        'controller_url = "https://c"\n'
        'site = "default"\n'
        'dialect = "classic"\n',
    )
    with pytest.raises(ConfigError) as exc_info:
        load_config(str(p))
    msg = str(exc_info.value)
    assert "classic" in msg
    assert "username" in msg
    assert "password_source" in msg
    assert "password_ref" in msg


def test_load_config_classic_missing_username_only(tmp_path):
    p = _write(
        tmp_path,
        'controller_url = "https://c"\n'
        'site = "default"\n'
        'dialect = "classic"\n'
        'password_source = "env"\n'
        'password_ref = "PW"\n',
    )
    with pytest.raises(ConfigError) as exc_info:
        load_config(str(p))
    msg = str(exc_info.value)
    assert "classic" in msg
    assert "username" in msg
    assert "password_ref" not in msg


def test_load_config_classic_missing_password_ref_only(tmp_path):
    p = _write(
        tmp_path,
        'controller_url = "https://c"\n'
        'site = "default"\n'
        'dialect = "classic"\n'
        'username = "admin"\n'
        'password_source = "env"\n',
    )
    with pytest.raises(ConfigError) as exc_info:
        load_config(str(p))
    msg = str(exc_info.value)
    assert "password_ref" in msg
    assert "username" not in msg


def test_load_config_classic_invalid_password_source(tmp_path):
    p = _write(
        tmp_path,
        'controller_url = "https://c"\n'
        'site = "default"\n'
        'dialect = "classic"\n'
        'username = "admin"\n'
        'password_source = "bogus"\n'
        'password_ref = "PW"\n',
    )
    with pytest.raises(ConfigError, match="password_source"):
        load_config(str(p))


def test_load_config_unifi_os_missing_api_key_source(tmp_path):
    p = _write(
        tmp_path,
        'controller_url = "https://unifi.example"\n'
        'site = "default"\n'
        'api_key_ref = "UNIFI_API_KEY"\n',
    )
    with pytest.raises(ConfigError) as exc_info:
        load_config(str(p))
    msg = str(exc_info.value)
    assert "unifi-os" in msg
    assert "api_key_source" in msg


def test_load_config_unifi_os_missing_api_key_ref(tmp_path):
    p = _write(
        tmp_path,
        'controller_url = "https://unifi.example"\n'
        'site = "default"\n'
        'api_key_source = "env"\n',
    )
    with pytest.raises(ConfigError, match="api_key_ref"):
        load_config(str(p))


def test_load_config_op_source_without_vault_raises_classic(tmp_path):
    p = _write(
        tmp_path,
        'controller_url = "https://c"\n'
        'site = "default"\n'
        'dialect = "classic"\n'
        'username = "admin"\n'
        'password_source = "op"\n'
        'password_ref = "op://vault/item/password"\n',
    )
    with pytest.raises(ConfigError, match="op_vault"):
        load_config(str(p))


def test_load_config_op_source_without_vault_raises_unifi_os(tmp_path):
    p = _write(
        tmp_path,
        'controller_url = "https://unifi.example"\n'
        'site = "default"\n'
        'api_key_source = "op"\n'
        'api_key_ref = "op://vault/item/key"\n',
    )
    with pytest.raises(ConfigError, match="op_vault"):
        load_config(str(p))


def test_load_config_op_source_with_vault_passes(tmp_path):
    p = _write(
        tmp_path,
        'controller_url = "https://unifi.example"\n'
        'site = "default"\n'
        'api_key_source = "op"\n'
        'api_key_ref = "op://vault/item/key"\n'
        'op_vault = "ExampleVault"\n',
    )
    cfg = load_config(str(p))
    assert cfg.op_vault == "ExampleVault"


def test_direct_config_construction_stays_unvalidated():
    # Validation lives in load_config, NOT __post_init__ — direct
    # construction (tests, library callers building partial configs) must
    # never raise, even for a nonsensical classic config.
    cfg = Config(controller_url="https://c", site="default", dialect="classic")
    assert cfg.username == ""
