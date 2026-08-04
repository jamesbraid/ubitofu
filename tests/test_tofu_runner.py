# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 James Braid
import importlib
import subprocess

import pytest

from ubitofu.errors import ExternalDocumentError, TofuExecutionError
from ubitofu.tofu_runner import TofuRunner


def test_runner_does_not_export_the_removed_tofu_error_alias():
    module = importlib.import_module("ubitofu.tofu_runner")

    assert not hasattr(module, "TofuError")


def _fake_run(record):
    def run(args, **kwargs):
        record.append(args)
        return subprocess.CompletedProcess(
            args, 0, stdout='{"format_version":"1.0","errored":false}', stderr=""
        )

    return run


def test_mutating_subcommands_refused(tmp_path):
    r = TofuRunner(workdir=tmp_path, _runner=_fake_run([]))
    with pytest.raises(TofuExecutionError, match="apply"):
        r._run(["apply", "-auto-approve"])
    with pytest.raises(TofuExecutionError, match="destroy"):
        r._run(["destroy", "-auto-approve"])
    with pytest.raises(TofuExecutionError, match="tofu state failed"):
        r._run(["state", "rm", "unifi_network.lan"])
    with pytest.raises(TofuExecutionError, match="refresh"):
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


def test_plan_generate_config_rejects_nonzero_and_removes_partial_stub(tmp_path):
    # A non-zero plan is never usable, even if OpenTofu left a partial stub.
    stub = tmp_path / "gen.tf"

    def run(args, **kwargs):
        stub.write_text('resource "unifi_x" "y" {\n  bad = "all"\n}\n')
        return subprocess.CompletedProcess(args, 1, stdout="", stderr="Invalid Attribute Value")

    r = TofuRunner(workdir=tmp_path, _runner=run)
    with pytest.raises(TofuExecutionError):
        r.plan(out=tmp_path / "tf.plan", generate_config_out=stub)
    assert not stub.exists()


def test_plan_generate_config_raises_when_stub_not_written(tmp_path):
    # A genuine failure (auth error, etc.) writes no stub -> must still raise.
    stub = tmp_path / "gen.tf"

    def run(args, **kwargs):
        return subprocess.CompletedProcess(args, 1, stdout="", stderr="auth boom")

    r = TofuRunner(workdir=tmp_path, _runner=run)
    with pytest.raises(TofuExecutionError, match="tofu plan failed"):
        r.plan(out=tmp_path / "tf.plan", generate_config_out=stub)


def test_plan_generate_config_raises_when_stub_empty(tmp_path):
    stub = tmp_path / "gen.tf"

    def run(args, **kwargs):
        stub.write_text("")  # written but empty -> not usable
        return subprocess.CompletedProcess(args, 1, stdout="", stderr="empty boom")

    r = TofuRunner(workdir=tmp_path, _runner=run)
    with pytest.raises(TofuExecutionError, match="tofu plan failed"):
        r.plan(out=tmp_path / "tf.plan", generate_config_out=stub)


def test_show_json_parses(tmp_path):
    def run(args, **kwargs):
        return subprocess.CompletedProcess(
            args, 0,
            stdout=(
                '{"format_version":"1.0","errored":false,'
                '"planned_values": {"root_module": {"resources": []}}}'
            ),
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
    with pytest.raises(TofuExecutionError, match="tofu providers failed"):
        r.providers_schema()


@pytest.mark.parametrize(
    ("method", "args", "returncode"),
    [
        ("plan", (), 1),
        ("plan", (), 3),
        ("show_json", ("tf.plan",), 2),
        ("show_state_json", (), 2),
        ("providers_schema", (), 2),
    ],
)
def test_only_detailed_plan_accepts_exit_two(tmp_path, method, args, returncode):
    def run(command, **kwargs):
        return subprocess.CompletedProcess(
            command, returncode, stdout="{}", stderr="provider stderr"
        )

    runner = TofuRunner(workdir=tmp_path, _runner=run)
    call = getattr(runner, method)
    with pytest.raises(TofuExecutionError):
        if method == "plan":
            call(out=tmp_path / "tf.plan")
        else:
            call(*(tmp_path / arg if arg == "tf.plan" else arg for arg in args))


@pytest.mark.parametrize(
    "stdout",
    [
        "not-json",
        '{"format_version":"2.0","errored":false}',
        '{"format_version":"1.0"}',
        '{"format_version":"1.0","errored":true}',
    ],
)
def test_show_json_rejects_invalid_plan_documents(tmp_path, stdout):
    def run(args, **kwargs):
        return subprocess.CompletedProcess(args, 0, stdout=stdout, stderr="")

    with pytest.raises(ExternalDocumentError):
        TofuRunner(workdir=tmp_path, _runner=run).show_json(tmp_path / "tf.plan")
