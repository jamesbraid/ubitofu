# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 James Braid
import subprocess

import pytest

from unifi_tofu_import.tofu_runner import TofuError, TofuRunner


def _fake_run(record):
    def run(args, **kwargs):
        record.append(args)
        return subprocess.CompletedProcess(args, 0, stdout='{"format_version":"1.0"}', stderr="")

    return run


def test_mutating_subcommands_refused(tmp_path):
    r = TofuRunner(workdir=tmp_path, _runner=_fake_run([]))
    with pytest.raises(TofuError, match="apply"):
        r._run(["apply", "-auto-approve"])
    with pytest.raises(TofuError, match="destroy"):
        r._run(["destroy", "-auto-approve"])
    with pytest.raises(TofuError, match="state rm"):
        r._run(["state", "rm", "unifi_network.lan"])
    with pytest.raises(TofuError, match="refresh"):
        r._run(["refresh"])  # refresh WRITES state -> forbidden


def test_readonly_subcommands_permitted(tmp_path):
    # Incremental mode needs read-only state inspection: these must NOT raise.
    calls = []
    r = TofuRunner(workdir=tmp_path, _runner=_fake_run(calls))
    r._run(["show", "-json"])  # current-state read
    r._run(["state", "list"])  # read-only state subcommand
    assert ["tofu", "show", "-json"] in calls
    assert ["tofu", "state", "list"] in calls


def test_plan_uses_detailed_exitcode_and_generate_config(tmp_path):
    calls = []
    r = TofuRunner(workdir=tmp_path, _runner=_fake_run(calls))
    r.plan(out=tmp_path / "tf.plan", generate_config_out=tmp_path / "gen.tf")
    args = calls[0]
    assert args[0] == "tofu"
    assert "plan" in args
    assert "-detailed-exitcode" in args
    assert any(a.startswith("-generate-config-out=") for a in args)
    assert "apply" not in args


def test_plan_generate_config_tolerates_nonzero_when_stub_written(tmp_path):
    # `tofu plan -generate-config-out` legitimately exits non-zero when the
    # generated stub has provider-invalid values, WHILE still writing the stub
    # (and the -out plan). Must NOT raise: the stub/plan are usable downstream.
    stub = tmp_path / "gen.tf"

    def run(args, **kwargs):
        stub.write_text('resource "unifi_x" "y" {\n  bad = "all"\n}\n')
        return subprocess.CompletedProcess(args, 1, stdout="", stderr="Invalid Attribute Value")

    r = TofuRunner(workdir=tmp_path, _runner=run)
    assert r.plan(out=tmp_path / "tf.plan", generate_config_out=stub) == 1


def test_plan_generate_config_raises_when_stub_not_written(tmp_path):
    # A genuine failure (auth error, etc.) writes no stub -> must still raise.
    stub = tmp_path / "gen.tf"

    def run(args, **kwargs):
        return subprocess.CompletedProcess(args, 1, stdout="", stderr="auth boom")

    r = TofuRunner(workdir=tmp_path, _runner=run)
    with pytest.raises(TofuError, match="auth boom"):
        r.plan(out=tmp_path / "tf.plan", generate_config_out=stub)


def test_plan_generate_config_raises_when_stub_empty(tmp_path):
    stub = tmp_path / "gen.tf"

    def run(args, **kwargs):
        stub.write_text("")  # written but empty -> not usable
        return subprocess.CompletedProcess(args, 1, stdout="", stderr="empty boom")

    r = TofuRunner(workdir=tmp_path, _runner=run)
    with pytest.raises(TofuError, match="empty boom"):
        r.plan(out=tmp_path / "tf.plan", generate_config_out=stub)


def test_show_json_parses(tmp_path):
    def run(args, **kwargs):
        return subprocess.CompletedProcess(
            args, 0,
            stdout='{"planned_values": {"root_module": {"resources": []}}}',
            stderr="",
        )

    r = TofuRunner(workdir=tmp_path, _runner=run)
    out = r.show_json(tmp_path / "tf.plan")
    assert out["planned_values"]["root_module"]["resources"] == []


def test_is_clean_maps_exit_codes(tmp_path):
    r = TofuRunner(workdir=tmp_path, _runner=_fake_run([]))
    assert r.is_clean(0) is True
    assert r.is_clean(2) is False


def test_run_raises_on_error_exit(tmp_path):
    def run(args, **kwargs):
        return subprocess.CompletedProcess(args, 1, stdout="", stderr="boom")

    r = TofuRunner(workdir=tmp_path, _runner=run)
    with pytest.raises(TofuError, match="boom"):
        r.providers_schema()
