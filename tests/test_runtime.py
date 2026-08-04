# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 James Braid
import errno
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from ubitofu.errors import UbitofuError
from ubitofu.runtime import generation_import_scaffold, runtime_session


def _mode(path: Path) -> int:
    return path.stat().st_mode & 0o777


def test_runtime_session_owns_private_files_and_exact_child_cleanup(tmp_path):
    with runtime_session(tmp_path) as session:
        assert _mode(session.private_root) == 0o700
        assert _mode(session.run_root.parent) == 0o700
        assert _mode(session.run_root) == 0o700
        assert not session.plan_path.exists()
        assert not session.generated_path.exists()
        assert session.run_root.parent == session.private_root / "tmp"
        session.plan_path.write_text("private plan")
        session.plan_path.chmod(0o600)
    assert not session.run_root.exists()
    assert (tmp_path / ".ubitofu").is_dir()


def test_runtime_session_releases_lock_after_exception(tmp_path):
    with pytest.raises(RuntimeError, match="caller failure"):
        with runtime_session(tmp_path):
            raise RuntimeError("caller failure")
    with runtime_session(tmp_path):
        pass


def test_runtime_session_excludes_another_process(tmp_path):
    worker = Path(__file__).parent / "helpers" / "runtime_worker.py"
    first = subprocess.Popen(
        [sys.executable, str(worker), str(tmp_path)], stdout=subprocess.PIPE, text=True
    )
    assert first.stdout is not None
    assert first.stdout.readline().strip() == "locked"
    second = subprocess.run(
        [sys.executable, str(worker), str(tmp_path), "nonblocking"], capture_output=True, text=True
    )
    first.terminate()
    first.wait(timeout=5)
    assert second.returncode == 3
    assert second.stdout.strip() == "blocked"


def test_runtime_session_recovers_valid_sigkill_residue(tmp_path):
    worker = Path(__file__).parent / "helpers" / "runtime_worker.py"
    process = subprocess.Popen(
        [sys.executable, str(worker), str(tmp_path)], stdout=subprocess.PIPE, text=True
    )
    assert process.stdout is not None
    assert process.stdout.readline().strip() == "locked"
    process.kill()
    process.wait(timeout=5)

    with runtime_session(tmp_path) as session:
        assert list(session.run_root.parent.iterdir()) == [session.run_root]


def test_runtime_session_refuses_symlinked_or_malformed_residue(tmp_path):
    private = tmp_path / ".ubitofu"
    private.mkdir(mode=0o700)
    (private / "tmp").symlink_to(tmp_path)
    with pytest.raises(UbitofuError):
        with runtime_session(tmp_path):
            pass
    (private / "tmp").unlink()
    residue = private / "tmp"
    residue.mkdir(mode=0o700)
    (residue / "not-a-session").mkdir(mode=0o700)
    with pytest.raises(UbitofuError):
        with runtime_session(tmp_path):
            pass


def test_runtime_session_refuses_a_symlinked_lock_file(tmp_path):
    private = tmp_path / ".ubitofu"
    private.mkdir(mode=0o700)
    (private / "tmp").mkdir(mode=0o700)
    target = tmp_path / "outside-lock"
    target.write_text("do not touch")
    (private / "lock").symlink_to(target)

    with pytest.raises(UbitofuError):
        with runtime_session(tmp_path):
            pass
    assert target.read_text() == "do not touch"


def test_runtime_session_leaves_malformed_child_bytes_untouched(tmp_path):
    run_root = tmp_path / ".ubitofu" / "tmp" / ("a" * 32)
    run_root.mkdir(parents=True, mode=0o700)
    expected = {
        ".ubitofu-manifest": b"tf.plan\ngenerated_stub.tf\n",
        "tf.plan": b"private plan",
        "generated_stub.tf": b"private generated stub",
        "unexpected": b"do not delete",
    }
    for name, value in expected.items():
        (run_root / name).write_bytes(value)

    with pytest.raises(UbitofuError) as exc_info:
        with runtime_session(tmp_path):
            pass

    assert (
        str(exc_info.value)
        == "unexpected internal error; rerun with local debug logging and report the command"
    )
    assert {path.name: path.read_bytes() for path in run_root.iterdir()} == expected


def test_generation_scaffold_is_private_durable_hardlink_and_cleans_root(tmp_path):
    content = b'import {\n  to = terraform_data.example\n  id = "synthetic"\n}\n'

    with runtime_session(tmp_path) as session:
        with generation_import_scaffold(session, content) as published:
            private = session.run_root / "generation-imports.tf"
            manifest = json.loads((session.run_root / ".ubitofu-manifest").read_text())
            assert published == tmp_path / "ubitofu-imports.tf"
            assert published.read_bytes() == content
            assert private.read_bytes() == content
            assert published.stat().st_ino == private.stat().st_ino
            assert published.stat().st_dev == private.stat().st_dev
            assert _mode(published) == _mode(private) == 0o600
            assert manifest["version"] == 2
            facts = manifest["scaffold"]
            assert facts["sha256"]
            assert facts["inode"] == private.stat().st_ino
            assert facts["device"] == private.stat().st_dev
            assert facts["uid"] == private.stat().st_uid
            assert facts["gid"] == private.stat().st_gid
            assert facts["mode"] == private.stat().st_mode
            assert facts["size"] == len(content)
        assert not published.exists()
        assert private.exists()
    assert not session.run_root.exists()


def test_generation_scaffold_sigkill_residue_recovers_only_exact_link(tmp_path):
    worker = Path(__file__).parent / "helpers" / "runtime_worker.py"
    process = subprocess.Popen(
        [sys.executable, str(worker), str(tmp_path), "scaffold"],
        stdout=subprocess.PIPE,
        text=True,
    )
    assert process.stdout is not None
    assert process.stdout.readline().strip() == "locked"
    published = tmp_path / "ubitofu-imports.tf"
    assert published.is_file()
    process.kill()
    process.wait(timeout=5)

    with runtime_session(tmp_path) as session:
        assert not published.exists()
        assert list(session.run_root.parent.iterdir()) == [session.run_root]


@pytest.mark.parametrize("mismatch", ["bytes", "inode", "mode", "symlink", "missing"])
def test_generation_scaffold_recovery_preserves_mismatched_evidence(
    tmp_path, mismatch
):
    worker = Path(__file__).parent / "helpers" / "runtime_worker.py"
    process = subprocess.Popen(
        [sys.executable, str(worker), str(tmp_path), "scaffold"],
        stdout=subprocess.PIPE,
        text=True,
    )
    assert process.stdout is not None
    assert process.stdout.readline().strip() == "locked"
    published = tmp_path / "ubitofu-imports.tf"
    process.kill()
    process.wait(timeout=5)
    residue = next((tmp_path / ".ubitofu" / "tmp").iterdir())
    private = residue / "generation-imports.tf"
    if mismatch == "bytes":
        published.write_bytes(b"changed evidence")
    elif mismatch == "inode":
        published.unlink()
        published.write_bytes(private.read_bytes())
        published.chmod(0o600)
    elif mismatch == "mode":
        published.chmod(0o640)
    elif mismatch == "symlink":
        published.unlink()
        published.symlink_to(private)
    else:
        private.unlink()

    with pytest.raises(UbitofuError):
        with runtime_session(tmp_path):
            pass

    assert os.path.lexists(published)
    assert residue.exists()


def test_generation_scaffold_refuses_reserved_path_without_manifest(tmp_path):
    reserved = tmp_path / "ubitofu-imports.tf"
    reserved.write_text("operator file")

    with pytest.raises(UbitofuError):
        with runtime_session(tmp_path):
            pass

    assert reserved.read_text() == "operator file"


def test_generation_scaffold_rejects_cross_filesystem_link_without_root_write(
    tmp_path, monkeypatch
):
    real_link = os.link

    def cross_device(source, destination, **kwargs):
        if Path(destination).name == "ubitofu-imports.tf":
            raise OSError(errno.EXDEV, "cross-device link")
        real_link(source, destination, **kwargs)

    monkeypatch.setattr(os, "link", cross_device)
    with runtime_session(tmp_path) as session:
        with pytest.raises(UbitofuError):
            with generation_import_scaffold(session, b"import {}\n"):
                pass
    assert not (tmp_path / "ubitofu-imports.tf").exists()
