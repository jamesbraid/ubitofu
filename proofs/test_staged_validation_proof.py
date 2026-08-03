# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 James Braid
from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import stat
import subprocess
from pathlib import Path

import pytest
from tools import staged_validation_proof as staged_validation
from tools.staged_validation_proof import (
    StagedValidationError,
    validate_staged_module,
    verify_offline_enforcement,
)


@pytest.fixture
def fixtures_dir() -> Path:
    return Path(__file__).resolve().parents[1] / "tests" / "fixtures"


def _expected_network_isolation() -> str:
    return {
        "Darwin": "macos sandbox-exec deny outbound IP sockets",
        "Linux": "linux unshare network namespace deny outbound IP sockets",
    }[platform.system()]


def _snapshot_tree(root: Path) -> dict[str, tuple[int, int, int, int, int, str]]:
    snapshot: dict[str, tuple[int, int, int, int, int, str]] = {}
    for path in sorted((root, *root.rglob("*"))):
        metadata = path.lstat()
        digest = ""
        if stat.S_ISREG(metadata.st_mode):
            try:
                digest = hashlib.sha256(path.read_bytes()).hexdigest()
            except PermissionError:
                digest = "unreadable"
        elif stat.S_ISLNK(metadata.st_mode):
            digest = os.readlink(path)
        snapshot[str(path.relative_to(root))] = (
            metadata.st_mode,
            metadata.st_ino,
            metadata.st_dev,
            metadata.st_size,
            metadata.st_mtime_ns,
            digest,
        )
    return snapshot


def _bootstrap_registry_dependencies(workdir: Path) -> None:
    process = subprocess.run(
        ["tofu", "init", "-backend=false", "-input=false", "-no-color"],
        cwd=workdir,
        check=False,
        capture_output=True,
        text=True,
        timeout=180,
    )
    if process.returncode != 0:
        pytest.fail(f"fixture bootstrap failed:\n{process.stdout}\n{process.stderr}")


def _copy_fixture(fixtures_dir: Path, tmp_path: Path) -> Path:
    fixture_root = fixtures_dir / "staged_validation"
    destination = tmp_path / "destination"
    shutil.copytree(fixture_root, destination)
    return destination / "project"


def _inject_stale_caches(workdir: Path) -> None:
    modules_path = workdir / ".terraform" / "modules" / "modules.json"
    modules = json.loads(modules_path.read_text())
    modules["Modules"].append(
        {
            "Key": "stale",
            "Source": "registry.opentofu.org/example/stale/null",
            "Version": "9.9.9",
            "Dir": ".terraform/modules/stale",
        }
    )
    modules_path.write_text(json.dumps(modules))
    stale_module = workdir / ".terraform" / "modules" / "stale"
    stale_module.mkdir()
    (stale_module / "main.tf").write_text('output "stale" { value = true }\n')

    stale_provider = (
        workdir
        / ".terraform"
        / "providers"
        / "registry.opentofu.org"
        / "example"
        / "stale"
        / "9.9.9"
        / "darwin_arm64"
    )
    stale_provider.mkdir(parents=True)
    (stale_provider / "tofu-provider-stale_v9.9.9").write_text("not a provider")
    with (workdir / ".terraform.lock.hcl").open("a") as lockfile:
        lockfile.write(
            '\nprovider "registry.opentofu.org/example/stale" {\n'
            '  version = "9.9.9"\n'
            '  hashes = ["h1:stale"]\n'
            '}\n'
        )


def _selected_registry_cache(workdir: Path) -> Path:
    manifest = json.loads(
        (workdir / ".terraform" / "modules" / "modules.json").read_text()
    )
    selected = next(entry for entry in manifest["Modules"] if entry["Key"] == "registry")
    return workdir / selected["Dir"]


def _replace_selected_registry_cache(workdir: Path) -> None:
    selected = _selected_registry_cache(workdir)
    for child in selected.iterdir():
        if child.is_dir():
            shutil.rmtree(child)
        else:
            child.unlink()
    (selected / "main.tf").write_text(
        'variable "namespace" { type = string }\n'
        'variable "name" { type = string }\n'
        'output "id" { value = "${var.namespace}-${var.name}" }\n'
    )
    (selected / "unrelated.txt").write_text("must not be staged")
    (selected / "terraform.tfstate").write_text('{"sensitive": true}')
    (selected / "secret.auto.tfvars").write_text('password = "secret"\n')
    git_dir = selected / ".git"
    git_dir.mkdir()
    (git_dir / "config").write_text("must not be staged")


def test_real_tofu_validates_complete_offline_stage_without_destination_writes(
    fixtures_dir: Path, tmp_path: Path
) -> None:
    workdir = _copy_fixture(fixtures_dir, tmp_path)
    _bootstrap_registry_dependencies(workdir)
    _replace_selected_registry_cache(workdir)
    _inject_stale_caches(workdir)
    (workdir / "active_tofu.tofu").write_text("this candidate target is invalid on disk\n")
    before = _snapshot_tree(workdir.parent)

    result = validate_staged_module(
        workdir,
        {Path("active_tofu.tofu"): b'output "candidate_seen" { value = "yes" }\n'},
        tofu_binary="tofu",
    )

    assert result.tofu_version.startswith("OpenTofu v1.12.0")
    assert result.root_files == (
        ".terraform.lock.hcl",
        "active_tf.tf",
        "active_tf.tf.json",
        "active_tofu.tofu",
        "active_tofu.tofu.json",
        "override.tofu",
        "proof_override.tofu.json",
        "shadowed.tofu",
        "shadowed.tofu.json",
        "versions.tf",
    )
    assert result.local_modules == (
        "../outside",
        "../outside/nested",
        "modules/inside",
        "modules/nested",
    )
    assert result.registry_modules == ("cloudposse/label/null@0.25.0",)
    assert result.providers == (
        "registry.opentofu.org/hashicorp/http@3.5.0",
        "registry.opentofu.org/hashicorp/random@3.7.2",
    )
    assert result.cache_paths == (
        ".terraform/modules/modules.json",
        ".terraform/modules/registry",
        ".terraform/providers/registry.opentofu.org/hashicorp/http/3.5.0",
        ".terraform/providers/registry.opentofu.org/hashicorp/random/3.7.2",
    )
    assert result.module_cache_files == (
        ".terraform/modules/modules.json",
        ".terraform/modules/registry/main.tf",
    )
    assert result.network_isolation == _expected_network_isolation()
    assert "Success! The configuration is valid." in result.stdout
    assert _snapshot_tree(workdir.parent) == before


def test_missing_dependency_cache_refuses_to_download(fixtures_dir: Path, tmp_path: Path) -> None:
    workdir = _copy_fixture(fixtures_dir, tmp_path)
    before = _snapshot_tree(workdir.parent)

    with pytest.raises(StagedValidationError, match="initialize first"):
        validate_staged_module(workdir, {}, tofu_binary="tofu")

    assert _snapshot_tree(workdir.parent) == before


def test_new_tofu_candidate_shadows_existing_tf_file(tmp_path: Path) -> None:
    workdir = tmp_path / "destination"
    workdir.mkdir()
    (workdir / "replacement.tf").write_text("this source file is invalid when selected\n")
    before = _snapshot_tree(workdir)

    result = validate_staged_module(
        workdir,
        {Path("replacement.tofu"): b'output "candidate_seen" { value = "yes" }\n'},
        tofu_binary="tofu",
    )

    assert result.root_files == ("replacement.tofu",)
    assert "Success! The configuration is valid." in result.stdout
    assert _snapshot_tree(workdir) == before


def test_offline_enforcement_denies_a_real_socket() -> None:
    assert verify_offline_enforcement() == _expected_network_isolation()


def test_offline_enforcement_rejects_sandbox_apply_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(staged_validation.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(
        staged_validation,
        "_sandbox_prefix",
        lambda: ["/usr/bin/sandbox-exec", "-p", "test-profile"],
    )

    def sandbox_apply_failure(*args, **kwargs):
        return subprocess.CompletedProcess(
            args[0],
            71,
            stdout="",
            stderr="sandbox-exec: sandbox_apply: Operation not permitted",
        )

    with pytest.raises(StagedValidationError, match="sandbox profile application failed"):
        verify_offline_enforcement(_runner=sandbox_apply_failure)


def test_linux_offline_enforcement_requires_exact_network_namespace_sentinel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(staged_validation.platform, "system", lambda: "Linux")
    monkeypatch.setattr(
        staged_validation.shutil, "which", lambda *args, **kwargs: "/usr/bin/unshare"
    )

    def denied(*args, **kwargs):
        command = args[0]
        assert command[:5] == [
            "/usr/bin/unshare",
            "--user",
            "--map-root-user",
            "--net",
            "--",
        ]
        return subprocess.CompletedProcess(
            command,
            73,
            stdout=(
                "UBITOFU_PARENT_NETNS_DENIED:EACCES\n"
                "UBITOFU_NETWORK_DENIED:ENETUNREACH\n"
            ),
            stderr="",
        )

    assert (
        verify_offline_enforcement(_runner=denied)
        == "linux unshare network namespace deny outbound IP sockets"
    )


def test_linux_offline_enforcement_rejects_missing_parent_namespace_denial(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(staged_validation.platform, "system", lambda: "Linux")
    monkeypatch.setattr(
        staged_validation.shutil, "which", lambda *args, **kwargs: "/usr/bin/unshare"
    )

    def escapable(*args, **kwargs):
        return subprocess.CompletedProcess(
            args[0],
            73,
            stdout="UBITOFU_NETWORK_DENIED:ENETUNREACH\n",
            stderr="",
        )

    with pytest.raises(StagedValidationError, match="parent namespace escape probe"):
        verify_offline_enforcement(_runner=escapable)


def test_linux_offline_enforcement_rejects_unshare_permission_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(staged_validation.platform, "system", lambda: "Linux")
    monkeypatch.setattr(
        staged_validation.shutil, "which", lambda *args, **kwargs: "/usr/bin/unshare"
    )

    def permission_failure(*args, **kwargs):
        return subprocess.CompletedProcess(
            args[0],
            1,
            stdout="",
            stderr="unshare: unshare failed: Operation not permitted",
        )

    with pytest.raises(StagedValidationError, match="Linux network namespace application failed"):
        verify_offline_enforcement(_runner=permission_failure)


def test_linux_offline_enforcement_rejects_missing_unshare(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(staged_validation.platform, "system", lambda: "Linux")
    monkeypatch.setattr(staged_validation.shutil, "which", lambda *args, **kwargs: None)

    with pytest.raises(StagedValidationError, match="Linux unshare is unavailable"):
        verify_offline_enforcement()


def test_override_module_source_replaces_primary_source(tmp_path: Path) -> None:
    workdir = tmp_path / "destination"
    safe = workdir / "safe"
    unused = tmp_path / "unused"
    safe.mkdir(parents=True)
    unused.mkdir()
    (safe / "main.tf").write_text('output "safe" { value = true }\n')
    (unused / "main.tf").write_text("this unused module must not be staged\n")
    (workdir / "main.tf").write_text('module "selected" { source = "../unused" }\n')
    (workdir / "override.tofu").write_text(
        'module "selected" { source = "./safe" }\n'
    )
    _bootstrap_registry_dependencies(workdir)
    before = _snapshot_tree(tmp_path)

    result = validate_staged_module(workdir, {}, tofu_binary="tofu")

    assert result.local_modules == ("safe",)
    assert "Success! The configuration is valid." in result.stdout
    assert _snapshot_tree(tmp_path) == before


def test_local_module_rejects_symlinked_ancestor(tmp_path: Path) -> None:
    workdir = tmp_path / "destination"
    child = tmp_path / "real-modules" / "child"
    workdir.mkdir()
    child.mkdir(parents=True)
    (child / "main.tf").write_text('output "child" { value = true }\n')
    (workdir / "modules").symlink_to(child.parent, target_is_directory=True)
    (workdir / "main.tf").write_text('module "child" { source = "./modules/child" }\n')
    before = _snapshot_tree(tmp_path)

    with pytest.raises(StagedValidationError, match="symlinked local module path"):
        validate_staged_module(workdir, {}, tofu_binary="tofu")

    assert _snapshot_tree(tmp_path) == before


@pytest.mark.parametrize(
    "function", ["file", "filebase64", "fileexists", "fileset", "templatefile"]
)
def test_filesystem_functions_fail_closed_before_tofu(tmp_path: Path, function: str) -> None:
    workdir = tmp_path / "destination"
    workdir.mkdir()
    arguments = '"/etc/hosts", "*"' if function == "fileset" else '"/etc/hosts"'
    (workdir / "main.tf").write_text(
        f'output "filesystem_escape" {{ value = {function}({arguments}) }}\n'
    )
    before = _snapshot_tree(workdir)

    with pytest.raises(StagedValidationError, match="filesystem functions are unsupported"):
        validate_staged_module(workdir, {}, tofu_binary="/tofu-must-not-run")

    assert _snapshot_tree(workdir) == before


def test_registry_module_filesystem_function_rejected_before_payload_read(
    fixtures_dir: Path, tmp_path: Path
) -> None:
    workdir = _copy_fixture(fixtures_dir, tmp_path)
    _bootstrap_registry_dependencies(workdir)
    selected = _selected_registry_cache(workdir)
    (selected / "escape.tf").write_text(
        'output "escape" { value = file("/etc/hosts") }\n'
    )
    sensitive = selected / "000-sensitive.bin"
    sensitive.write_text("must not be read or copied")
    sensitive.chmod(0)
    before = _snapshot_tree(workdir.parent)

    try:
        with pytest.raises(StagedValidationError, match="filesystem functions are unsupported"):
            validate_staged_module(workdir, {}, tofu_binary="/tofu-must-not-run")
        assert _snapshot_tree(workdir.parent) == before
    finally:
        sensitive.chmod(0o600)


@pytest.mark.parametrize(
    ("config_name", "outside_content"),
    [
        ("main.tf", 'output "outside" { value = true }\n'),
        ("active.tf.json", '{"output":{"outside":{"value":true}}}\n'),
    ],
)
def test_registry_module_rejects_active_config_symlink_before_read(
    fixtures_dir: Path,
    tmp_path: Path,
    config_name: str,
    outside_content: str,
) -> None:
    workdir = _copy_fixture(fixtures_dir, tmp_path)
    _bootstrap_registry_dependencies(workdir)
    selected = _selected_registry_cache(workdir)
    config = selected / config_name
    if config.exists():
        config.unlink()
    outside = tmp_path / f"outside-{config_name.replace('.', '-')}"
    outside.write_text(outside_content)
    outside.chmod(0)
    config.symlink_to(outside)
    before = _snapshot_tree(tmp_path)

    try:
        with pytest.raises(StagedValidationError, match="symlinked module configuration"):
            validate_staged_module(workdir, {}, tofu_binary="/tofu-must-not-run")
        assert _snapshot_tree(tmp_path) == before
    finally:
        outside.chmod(0o600)


def test_registry_module_rejects_absolute_cache_dir_without_destination_write(
    fixtures_dir: Path, tmp_path: Path
) -> None:
    workdir = _copy_fixture(fixtures_dir, tmp_path)
    _bootstrap_registry_dependencies(workdir)
    modules_path = workdir / ".terraform" / "modules" / "modules.json"
    modules = json.loads(modules_path.read_text())
    selected = next(entry for entry in modules["Modules"] if entry["Key"] == "registry")
    selected["Dir"] = str(workdir / selected["Dir"])
    modules_path.write_text(json.dumps(modules))
    before = _snapshot_tree(workdir.parent)

    with pytest.raises(StagedValidationError, match="registry module cache Dir must be relative"):
        validate_staged_module(workdir, {}, tofu_binary="/tofu-must-not-run")

    assert _snapshot_tree(workdir.parent) == before


@pytest.mark.parametrize(
    ("target_name", "expected"),
    [
        ("main.tf", "root configuration"),
        (".terraform.lock.hcl", "provider lock file"),
    ],
)
def test_root_inputs_reject_symlink_before_read(
    tmp_path: Path, target_name: str, expected: str
) -> None:
    workdir = tmp_path / "destination"
    workdir.mkdir()
    if target_name != "main.tf":
        (workdir / "main.tf").write_text('output "valid" { value = true }\n')
    outside = tmp_path / f"outside-{target_name.replace('.', '-')}"
    outside.write_text('output "outside" { value = true }\n')
    outside.chmod(0)
    (workdir / target_name).symlink_to(outside)
    before = _snapshot_tree(tmp_path)

    try:
        with pytest.raises(StagedValidationError, match=f"symlinked {expected}"):
            validate_staged_module(workdir, {}, tofu_binary="/tofu-must-not-run")
        assert _snapshot_tree(tmp_path) == before
    finally:
        outside.chmod(0o600)


def test_module_manifest_rejects_symlink_before_read(
    fixtures_dir: Path, tmp_path: Path
) -> None:
    workdir = _copy_fixture(fixtures_dir, tmp_path)
    _bootstrap_registry_dependencies(workdir)
    manifest = workdir / ".terraform" / "modules" / "modules.json"
    outside = tmp_path / "outside-modules.json"
    outside.write_bytes(manifest.read_bytes())
    manifest.unlink()
    outside.chmod(0)
    manifest.symlink_to(outside)
    before = _snapshot_tree(workdir.parent)

    try:
        with pytest.raises(StagedValidationError, match="symlinked module manifest"):
            validate_staged_module(workdir, {}, tofu_binary="/tofu-must-not-run")
        assert _snapshot_tree(workdir.parent) == before
    finally:
        outside.chmod(0o600)


def test_provider_package_rejects_symlink_before_copy(
    fixtures_dir: Path, tmp_path: Path
) -> None:
    workdir = _copy_fixture(fixtures_dir, tmp_path)
    _bootstrap_registry_dependencies(workdir)
    provider = (
        workdir
        / ".terraform"
        / "providers"
        / "registry.opentofu.org"
        / "hashicorp"
        / "http"
        / "3.5.0"
    )
    outside = tmp_path / "outside-provider-secret"
    outside.write_text("must not be copied")
    outside.chmod(0)
    (provider / "outside-link").symlink_to(outside)
    before = _snapshot_tree(workdir.parent)

    try:
        with pytest.raises(StagedValidationError, match="symlinked provider cache entry"):
            validate_staged_module(workdir, {}, tofu_binary="/tofu-must-not-run")
        assert _snapshot_tree(workdir.parent) == before
    finally:
        outside.chmod(0o600)


def test_provider_source_traversal_rejected_before_copy_or_tofu(tmp_path: Path) -> None:
    workdir = tmp_path / "destination"
    workdir.mkdir()
    source = "registry.opentofu.org/../../outside-provider"
    (workdir / "main.tf").write_text(
        "terraform {\n"
        "  required_providers {\n"
        f'    escape = {{ source = "{source}", version = "1.0.0" }}\n'
        "  }\n"
        "}\n"
        'provider "escape" {}\n'
    )
    (workdir / ".terraform.lock.hcl").write_text(
        f'provider "{source}" {{\n'
        '  version = "1.0.0"\n'
        '  hashes = ["h1:escape"]\n'
        '}\n'
    )
    escaped = workdir / ".terraform" / "outside-provider" / "1.0.0"
    escaped.mkdir(parents=True)
    sensitive = escaped / "sensitive.bin"
    sensitive.write_text("must not be copied")
    sensitive.chmod(0)
    before = _snapshot_tree(workdir)

    try:
        with pytest.raises(StagedValidationError, match="invalid provider source address"):
            validate_staged_module(workdir, {}, tofu_binary="/tofu-must-not-run")
        assert _snapshot_tree(workdir) == before
    finally:
        sensitive.chmod(0o600)
