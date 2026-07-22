# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 James Braid
"""S9: unreachable controller URL exits 1 (no container needed — this test
must never stop the shared session containers). S10 (verify outcomes) is
parked with the write scenarios: docs/provider-import-bugs.md."""
import pytest

pytestmark = pytest.mark.controller


def test_s9_unreachable_controller_exits_1(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("UNIFI_TEST_PASSWORD_S9", "irrelevant")
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        'controller_url = "https://127.0.0.1:1"\n'
        'site = "default"\n'
        'dialect = "classic"\n'
        'username = "admin"\n'
        'password_source = "env"\n'
        'password_ref = "UNIFI_TEST_PASSWORD_S9"\n'
        f'workdir = "{tmp_path}"\n'
    )
    from ubitofu.cli import main
    code = main(["reconcile", "--config", str(cfg)])
    err = capsys.readouterr().err
    assert code == 1
    assert "cannot reach" in err
