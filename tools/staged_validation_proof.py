# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 James Braid
"""Evidence harness for faithful, offline staged OpenTofu validation.

This module proves the staged-validation contract. It is not production runtime
code and must not be imported by the ubitofu package.
"""

from __future__ import annotations

import json
import os
import platform
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import hcl2  # type: ignore[import-untyped]

_CONFIG_SUFFIXES = (".tofu.json", ".tf.json", ".tofu", ".tf")
_LOCKFILE = ".terraform.lock.hcl"
_LOCAL_PREFIXES = ("./", "../")
_SANDBOX_PROFILE = (
    '(version 1)(allow default)(deny network-outbound (remote ip "*:*") )'
)
_MACOS_NETWORK_ISOLATION = "macos sandbox-exec deny outbound IP sockets"
_LINUX_NETWORK_ISOLATION = "linux unshare network namespace deny outbound IP sockets"
_FILESYSTEM_FUNCTION = re.compile(r"\b(?:file\w*|fileset|templatefile)\s*\(")
_LOCK_PROVIDER = re.compile(
    r'^provider\s+"(?P<source>[^"]+)"\s*\{.*?^\s*version\s*=\s*"(?P<version>[^"]+)"',
    re.MULTILINE | re.DOTALL,
)
_LOCK_PROVIDER_BLOCK = re.compile(
    r'^provider\s+"(?P<source>[^"]+)"\s*\{.*?^\}\s*', re.MULTILINE | re.DOTALL
)
_PROVIDER_HOSTNAME = re.compile(
    r"[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?"
)
_PROVIDER_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*")
_PROVIDER_VERSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9.+_-]*")


class StagedValidationError(RuntimeError):
    """The candidate module could not be validated under the proof contract."""


@dataclass(frozen=True)
class StagedValidationResult:
    tofu_version: str
    root_files: tuple[str, ...]
    local_modules: tuple[str, ...]
    registry_modules: tuple[str, ...]
    providers: tuple[str, ...]
    cache_paths: tuple[str, ...]
    module_cache_files: tuple[str, ...]
    network_isolation: str
    stdout: str


def _config_identity(name: str) -> tuple[str, str, bool] | None:
    for suffix in _CONFIG_SUFFIXES:
        if name.endswith(suffix):
            language = "json" if suffix.endswith(".json") else "native"
            family = "tofu" if suffix.startswith(".tofu") else "tf"
            return name[: -len(suffix)], language, family == "tofu"
    return None


def _active_names(names: set[str]) -> tuple[str, ...]:
    grouped: dict[tuple[str, str], dict[bool, str]] = {}
    for name in names:
        identity = _config_identity(name)
        if identity is None:
            continue
        stem, language, is_tofu = identity
        grouped.setdefault((stem, language), {})[is_tofu] = name
    return tuple(
        sorted(
            variants[True] if True in variants else variants[False]
            for variants in grouped.values()
        )
    )


def _root_inputs(workdir: Path, candidates: Mapping[Path, bytes]) -> dict[str, bytes]:
    disk_names = {
        path.name for path in workdir.iterdir() if _config_identity(path.name) is not None
    }
    candidate_names: set[str] = set()
    for relative, content in candidates.items():
        if (
            relative.is_absolute()
            or len(relative.parts) != 1
            or _config_identity(relative.name) is None
        ):
            raise StagedValidationError(
                f"candidate must be a root .tf, .tofu, .tf.json, or .tofu.json file: {relative}"
            )
        if not isinstance(content, bytes):
            raise StagedValidationError(f"candidate content must be bytes: {relative}")
        candidate_names.add(relative.name)

    selected: dict[str, bytes] = {}
    for name in _active_names(disk_names | candidate_names):
        relative = Path(name)
        selected[name] = (
            candidates[relative]
            if relative in candidates
            else _read_regular_no_follow(workdir / name, "root configuration")
        )
    lockfile = workdir / _LOCKFILE
    try:
        lockfile.lstat()
    except FileNotFoundError:
        pass
    else:
        selected[_LOCKFILE] = _read_regular_no_follow(lockfile, "provider lock file")
    return selected


def _parse_document(name: str, content: bytes) -> dict[str, Any]:
    try:
        if name.endswith(".json"):
            document = cast(dict[str, Any], json.loads(content))
        else:
            document = cast(dict[str, Any], hcl2.loads(content.decode("utf-8")))
    except (UnicodeDecodeError, json.JSONDecodeError, RuntimeError, ValueError) as exc:
        raise StagedValidationError(f"cannot parse active configuration {name}: {exc}") from exc
    _reject_filesystem_functions(name, document)
    return document


def _reject_filesystem_functions(name: str, value: Any) -> None:
    if isinstance(value, str) and _FILESYSTEM_FUNCTION.search(value):
        raise StagedValidationError(
            f"filesystem functions are unsupported in the staged closure: {name}"
        )
    if isinstance(value, dict):
        for child in value.values():
            _reject_filesystem_functions(name, child)
    elif isinstance(value, list):
        for child in value:
            _reject_filesystem_functions(name, child)


def _hcl_literal(value: str) -> str:
    if value.startswith('"') and value.endswith('"'):
        return cast(str, json.loads(value))
    return value


def _module_blocks(name: str, content: bytes) -> dict[str, str | None]:
    document = _parse_document(name, content)

    modules = document.get("module", {})
    blocks_by_name: dict[str, str | None] = {}
    if isinstance(modules, dict):
        entries: list[tuple[str, Any]] = list(modules.items())
    elif isinstance(modules, list):
        entries = []
        for entry in modules:
            if isinstance(entry, dict):
                entries.extend(entry.items())
    else:
        entries = []
    for label, block in entries:
        if not isinstance(block, dict):
            continue
        raw_source = block.get("source")
        source = _hcl_literal(raw_source) if isinstance(raw_source, str) else None
        blocks_by_name[_hcl_literal(label)] = source
    return blocks_by_name


def _is_override_file(name: str) -> bool:
    identity = _config_identity(name)
    if identity is None:
        return False
    stem = identity[0]
    return stem == "override" or stem.endswith("_override")


def _effective_module_blocks(inputs: Mapping[str, bytes]) -> dict[str, str]:
    effective: dict[str, str | None] = {}
    overrides: list[tuple[str, dict[str, str | None]]] = []
    for name in sorted(inputs):
        if _config_identity(name) is None:
            continue
        blocks = _module_blocks(name, inputs[name])
        if _is_override_file(name):
            overrides.append((name, blocks))
            continue
        for label, source in blocks.items():
            if label in effective:
                raise StagedValidationError(f"duplicate primary module block: {label}")
            effective[label] = source

    for name, blocks in overrides:
        for label, source in blocks.items():
            if label not in effective:
                raise StagedValidationError(
                    f"override module has no primary definition in {name}: {label}"
                )
            if source is not None:
                effective[label] = source
    missing = sorted(label for label, source in effective.items() if source is None)
    if missing:
        raise StagedValidationError(f"module source cannot be determined: {', '.join(missing)}")
    return {label: cast(str, source) for label, source in effective.items()}


def _read_module_config(source_dir: Path) -> dict[str, bytes]:
    names = {path.name for path in source_dir.iterdir() if _config_identity(path.name)}
    selected = {
        name: _read_config_no_follow(source_dir / name)
        for name in _active_names(names)
    }
    if not selected:
        raise StagedValidationError(f"local module has no active configuration: {source_dir}")
    return selected


def _read_config_no_follow(path: Path) -> bytes:
    return _read_regular_no_follow(path, "module configuration")


def _read_regular_no_follow(path: Path, description: str) -> bytes:
    metadata = path.lstat()
    if stat.S_ISLNK(metadata.st_mode):
        raise StagedValidationError(f"symlinked {description}: {path}")
    if not stat.S_ISREG(metadata.st_mode):
        raise StagedValidationError(f"{description} is not a regular file: {path}")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise StagedValidationError(f"cannot safely open {description}: {path}") from exc
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or (
            opened.st_dev,
            opened.st_ino,
        ) != (metadata.st_dev, metadata.st_ino):
            raise StagedValidationError(f"{description} changed while opening: {path}")
        with os.fdopen(descriptor, "rb", closefd=False) as config_file:
            return config_file.read()
    finally:
        os.close(descriptor)


def _write_module_config(stage_dir: Path, selected: Mapping[str, bytes]) -> None:
    stage_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    for name, content in selected.items():
        (stage_dir / name).write_bytes(content)


def _normalized_absolute(path: Path) -> Path:
    return Path(os.path.abspath(path))


def _assert_no_symlink_components(path: Path) -> None:
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        try:
            if current.is_symlink():
                raise StagedValidationError(f"symlinked local module path: {current}")
        except OSError as exc:
            raise StagedValidationError(f"cannot inspect local module path: {current}") from exc


def _required_provider_sources(inputs: Mapping[str, bytes]) -> set[str]:
    required: dict[str, str] = {}
    used: set[str] = set()
    for name, content in inputs.items():
        if _config_identity(name) is None:
            continue
        document = _parse_document(name, content)
        terraform = document.get("terraform", [])
        terraform_blocks = terraform if isinstance(terraform, list) else [terraform]
        for block in terraform_blocks:
            if not isinstance(block, dict):
                continue
            raw_required = block.get("required_providers", {})
            required_blocks = raw_required if isinstance(raw_required, list) else [raw_required]
            for declarations in required_blocks:
                if not isinstance(declarations, dict):
                    continue
                if _is_override_file(name) and declarations:
                    raise StagedValidationError(
                        "required_providers in override files are unsupported by the proof"
                    )
                for local_name, specification in declarations.items():
                    if local_name == "__is_block__" or not isinstance(specification, dict):
                        continue
                    raw_source = specification.get("source")
                    source = (
                        _hcl_literal(raw_source)
                        if isinstance(raw_source, str)
                        else f"hashicorp/{local_name}"
                    )
                    if source.count("/") == 1:
                        source = f"registry.opentofu.org/{source}"
                    required[_hcl_literal(local_name)] = source

        for kind in ("resource", "data"):
            collection = document.get(kind, [])
            entries = collection.items() if isinstance(collection, dict) else (
                pair
                for entry in collection if isinstance(entry, dict)
                for pair in entry.items()
            )
            for resource_type, _body in entries:
                local_name = _hcl_literal(resource_type).split("_", 1)[0]
                if local_name != "terraform":
                    used.add(local_name)
        providers = document.get("provider", [])
        provider_entries = providers.items() if isinstance(providers, dict) else (
            pair
            for entry in providers if isinstance(entry, dict)
            for pair in entry.items()
        )
        used.update(_hcl_literal(local_name) for local_name, _body in provider_entries)

    undeclared = sorted(used - required.keys())
    if undeclared:
        raise StagedValidationError(
            "explicit required_providers sources are required by the proof: "
            + ", ".join(undeclared)
        )
    return set(required.values())


def _module_manifest(workdir: Path) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    path = workdir / ".terraform" / "modules" / "modules.json"
    try:
        path.lstat()
    except FileNotFoundError:
        return [], {}
    try:
        document = cast(
            dict[str, Any], json.loads(_read_regular_no_follow(path, "module manifest"))
        )
    except (json.JSONDecodeError, ValueError) as exc:
        raise StagedValidationError("module cache metadata is invalid; initialize first") from exc
    entries = [entry for entry in document.get("Modules", []) if isinstance(entry, dict)]
    by_key = {
        cast(str, entry["Key"]): entry
        for entry in entries
        if isinstance(entry.get("Key"), str)
    }
    return entries, by_key


def _source_display(source: str) -> str:
    return source.removeprefix("registry.opentofu.org/")


def _stage_modules(
    *,
    workdir: Path,
    stage_root: Path,
    stage_fs: Path,
    root_inputs: Mapping[str, bytes],
) -> tuple[
    tuple[str, ...],
    tuple[str, ...],
    set[str],
    tuple[str, ...],
    tuple[str, ...],
]:
    manifest_entries, manifest_by_key = _module_manifest(workdir)
    queue: list[tuple[Path, Path, Mapping[str, bytes], str]] = [
        (workdir, stage_root, root_inputs, "")
    ]
    seen = {_normalized_absolute(workdir)}
    local_display: set[str] = set()
    registry_modules: set[str] = set()
    required_providers: set[str] = set()
    required_manifest_keys: set[str] = {""}
    copied_registry_dirs: set[str] = set()

    while queue:
        source_dir, staged_dir, inputs, module_key = queue.pop(0)
        required_providers.update(_required_provider_sources(inputs))
        for label, source in _effective_module_blocks(inputs).items():
            child_key = f"{module_key}.{label}" if module_key else label
            if source.startswith(_LOCAL_PREFIXES):
                source_path = _normalized_absolute(source_dir / source)
                _assert_no_symlink_components(source_path)
                if not source_path.is_dir():
                    raise StagedValidationError(f"local module does not exist: {source}")
                entry = manifest_by_key.get(child_key)
                if entry is None:
                    raise StagedValidationError("module cache is incomplete; initialize first")
                required_manifest_keys.add(child_key)
                staged_path = _normalized_absolute(staged_dir / source)
                if os.path.commonpath((stage_fs, staged_path)) != str(stage_fs):
                    raise StagedValidationError(f"local module escapes staged filesystem: {source}")
                relative_display = os.path.relpath(source_path, workdir)
                local_display.add(relative_display)
                if source_path not in seen:
                    seen.add(source_path)
                    child_inputs = _read_module_config(source_path)
                    _required_provider_sources(child_inputs)
                    _effective_module_blocks(child_inputs)
                    _write_module_config(staged_path, child_inputs)
                    queue.append((source_path, staged_path, child_inputs, child_key))
                continue
            if Path(source).is_absolute():
                raise StagedValidationError(
                    "absolute local module sources are unsupported by the offline stage: "
                    f"{source}"
                )

            entry = manifest_by_key.get(child_key)
            if entry is None:
                raise StagedValidationError("module cache is incomplete; initialize first")
            required_manifest_keys.add(child_key)
            entry_source = entry.get("Source")
            entry_version = entry.get("Version")
            entry_dir = entry.get("Dir")
            if (
                not isinstance(entry_source, str)
                or _source_display(entry_source) != _source_display(source)
                or not isinstance(entry_version, str)
                or not isinstance(entry_dir, str)
            ):
                raise StagedValidationError("registry module cache is incomplete; initialize first")
            entry_path = Path(entry_dir)
            if entry_path.is_absolute():
                raise StagedValidationError("registry module cache Dir must be relative")
            normalized_entry = Path(os.path.normpath(entry_dir))
            if normalized_entry != entry_path:
                raise StagedValidationError("registry module cache Dir must be normalized")
            cache_source = _normalized_absolute(workdir / normalized_entry)
            _assert_no_symlink_components(cache_source)
            modules_root = _normalized_absolute(workdir / ".terraform" / "modules")
            if cache_source == modules_root or not cache_source.is_relative_to(modules_root):
                raise StagedValidationError("registry module cache path escapes .terraform/modules")
            cache_target = _normalized_absolute(stage_root / normalized_entry)
            staged_modules_root = _normalized_absolute(stage_root / ".terraform" / "modules")
            if cache_target == staged_modules_root or not cache_target.is_relative_to(
                staged_modules_root
            ):
                raise StagedValidationError(
                    "registry module cache target escapes staged .terraform/modules"
                )
            child_inputs = _read_module_config(cache_source)
            _required_provider_sources(child_inputs)
            _effective_module_blocks(child_inputs)
            if entry_dir not in copied_registry_dirs:
                _write_module_config(cache_target, child_inputs)
                copied_registry_dirs.add(entry_dir)
            registry_modules.add(f"{_source_display(entry_source)}@{entry_version}")
            queue.append((cache_source, cache_target, child_inputs, child_key))

    if required_manifest_keys - {""}:
        filtered = [
            entry
            for entry in manifest_entries
            if isinstance(entry.get("Key"), str) and entry["Key"] in required_manifest_keys
        ]
        if not any(entry.get("Key") == "" for entry in filtered):
            filtered.insert(0, {"Key": "", "Source": "", "Dir": "."})
        modules_target = stage_root / ".terraform" / "modules"
        modules_target.mkdir(parents=True, exist_ok=True)
        (modules_target / "modules.json").write_text(
            json.dumps({"Modules": filtered}, separators=(",", ":")), encoding="utf-8"
        )
    cache_paths = tuple(
        sorted(
            (".terraform/modules/modules.json", *copied_registry_dirs)
            if required_manifest_keys - {""}
            else copied_registry_dirs
        )
    )
    modules_target = stage_root / ".terraform" / "modules"
    module_cache_files = tuple(
        sorted(
            str(path.relative_to(stage_root))
            for path in modules_target.rglob("*")
            if path.is_file()
        )
    ) if modules_target.is_dir() else ()
    return (
        tuple(sorted(local_display)),
        tuple(sorted(registry_modules)),
        required_providers,
        cache_paths,
        module_cache_files,
    )


def _copy_provider_cache(
    workdir: Path, stage_root: Path, required_sources: set[str]
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    lockfile = workdir / _LOCKFILE
    try:
        lockfile.lstat()
    except FileNotFoundError:
        lock_text = ""
    else:
        lock_text = _read_regular_no_follow(lockfile, "provider lock file").decode("utf-8")
    if required_sources and not lock_text:
        raise StagedValidationError("provider lock file is missing; initialize first")
    locked = {
        match.group("source"): match.group("version")
        for match in _LOCK_PROVIDER.finditer(lock_text)
    }
    if lock_text:
        blocks = list(_LOCK_PROVIDER_BLOCK.finditer(lock_text))
        header = lock_text[: blocks[0].start()] if blocks else lock_text
        filtered_blocks = [
            match.group(0).rstrip()
            for match in blocks
            if match.group("source") in required_sources
        ]
        (stage_root / _LOCKFILE).write_text(
            header.rstrip() + "\n\n" + "\n\n".join(filtered_blocks) + "\n",
            encoding="utf-8",
        )
    providers: set[str] = set()
    cache_paths: set[str] = set()
    for source in sorted(required_sources):
        source_parts = _provider_source_parts(source)
        version = locked.get(source)
        if version is None:
            raise StagedValidationError(f"provider lock is incomplete; initialize first: {source}")
        if not _PROVIDER_VERSION.fullmatch(version) or version in {".", ".."}:
            raise StagedValidationError(f"invalid provider version: {version}")
        provider_root = _normalized_absolute(workdir / ".terraform" / "providers")
        source_path = _normalized_absolute(provider_root / Path(*source_parts) / version)
        if source_path == provider_root or not source_path.is_relative_to(provider_root):
            raise StagedValidationError("provider cache source escapes .terraform/providers")
        _assert_no_symlink_components(source_path)
        if not source_path.is_dir():
            raise StagedValidationError(f"provider cache is missing; initialize first: {source}")
        staged_provider_root = _normalized_absolute(stage_root / ".terraform" / "providers")
        target = _normalized_absolute(staged_provider_root / Path(*source_parts) / version)
        if target == staged_provider_root or not target.is_relative_to(staged_provider_root):
            raise StagedValidationError("provider cache target escapes staged .terraform/providers")
        relative = target.relative_to(stage_root)
        target.parent.mkdir(parents=True, exist_ok=True)
        _assert_tree_has_no_symlinks(source_path, "provider cache entry")
        shutil.copytree(source_path, target, symlinks=True)
        _assert_tree_has_no_symlinks(target, "staged provider cache entry")
        providers.add(f"{source}@{version}")
        cache_paths.add(str(relative))
    return tuple(sorted(providers)), tuple(sorted(cache_paths))


def _provider_source_parts(source: str) -> tuple[str, str, str]:
    if "\\" in source:
        raise StagedValidationError(f"invalid provider source address: {source}")
    parts = source.split("/")
    if len(parts) != 3 or any(part in {"", ".", ".."} for part in parts):
        raise StagedValidationError(f"invalid provider source address: {source}")
    hostname, namespace, provider_type = parts
    if (
        not _PROVIDER_HOSTNAME.fullmatch(hostname)
        or ".." in hostname
        or not _PROVIDER_NAME.fullmatch(namespace)
        or not _PROVIDER_NAME.fullmatch(provider_type)
    ):
        raise StagedValidationError(f"invalid provider source address: {source}")
    return hostname, namespace, provider_type


def _assert_tree_has_no_symlinks(root: Path, description: str) -> None:
    _assert_no_symlink_components(root)
    for directory, subdirectories, filenames in os.walk(root, followlinks=False):
        for name in (*subdirectories, *filenames):
            path = Path(directory) / name
            if path.is_symlink():
                raise StagedValidationError(f"symlinked {description}: {path}")


def _sandbox_prefix() -> list[str]:
    system = platform.system()
    if system == "Darwin":
        sandbox = Path("/usr/bin/sandbox-exec")
        if not sandbox.is_file():
            raise StagedValidationError("macOS sandbox-exec is unavailable; gate remains open")
        return [str(sandbox), "-p", _SANDBOX_PROFILE]
    if system == "Linux":
        unshare = shutil.which("unshare", path="/usr/sbin:/usr/bin:/sbin:/bin")
        if unshare is None:
            raise StagedValidationError("Linux unshare is unavailable; gate remains open")
        return [unshare, "--user", "--map-root-user", "--net", "--"]
    raise StagedValidationError(
        "offline network enforcement is unsupported on this platform; gate remains open"
    )


def verify_offline_enforcement(
    *,
    _runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> str:
    """Prove that the platform command wrapper denies an actual socket connection."""
    system = platform.system()
    if system == "Darwin":
        expected_errno = "EPERM"
        network_isolation = _MACOS_NETWORK_ISOLATION
        python = "/usr/bin/python3"
    elif system == "Linux":
        expected_errno = "ENETUNREACH"
        network_isolation = _LINUX_NETWORK_ISOLATION
        python = sys.executable
    else:
        _sandbox_prefix()
        raise AssertionError("unreachable")
    sentinel = f"UBITOFU_NETWORK_DENIED:{expected_errno}"
    parent_namespace_probe = ""
    if system == "Linux":
        parent_namespace_probe = (
            'try:\n'
            '    parent_netns = os.open("/proc/1/ns/net", os.O_RDONLY)\n'
            'except OSError as exc:\n'
            '    if exc.errno not in (errno.EACCES, errno.EPERM):\n'
            '        raise\n'
            '    print(f"UBITOFU_PARENT_NETNS_DENIED:{errno.errorcode[exc.errno]}")\n'
            'else:\n'
            '    try:\n'
            '        os.setns(parent_netns, os.CLONE_NEWNET)\n'
            '    except OSError as exc:\n'
            '        if exc.errno not in (errno.EACCES, errno.EPERM):\n'
            '            raise\n'
            '        print(f"UBITOFU_PARENT_NETNS_DENIED:{errno.errorcode[exc.errno]}")\n'
            '    else:\n'
            '        raise SystemExit(75)\n'
        )
    probe = (
        "import errno, os, socket, sys\n"
        + parent_namespace_probe
        + "try:\n"
        '    socket.create_connection(("127.0.0.1", 1), 1)\n'
        "except OSError as exc:\n"
        f"    if exc.errno == errno.{expected_errno}:\n"
        f'        print("{sentinel}")\n'
        "        raise SystemExit(73)\n"
        "    raise\n"
        "raise SystemExit(74)\n"
    )
    process = _runner(
        [
            *_sandbox_prefix(),
            python,
            "-c",
            probe,
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    diagnostic = f"{process.stdout}\n{process.stderr}"
    if "sandbox_apply" in diagnostic:
        raise StagedValidationError(
            "macOS sandbox profile application failed: " + diagnostic.strip()
        )
    if system == "Linux" and "unshare:" in diagnostic:
        raise StagedValidationError(
            "Linux network namespace application failed: " + diagnostic.strip()
        )
    if system == "Linux":
        output_lines = process.stdout.splitlines()
        parent_denial = {
            "UBITOFU_PARENT_NETNS_DENIED:EACCES",
            "UBITOFU_PARENT_NETNS_DENIED:EPERM",
        }
        if len(output_lines) != 2 or output_lines[0] not in parent_denial:
            raise StagedValidationError(
                "Linux parent namespace escape probe did not fail closed: "
                + diagnostic.strip()
            )
        output_matches = output_lines[1] == sentinel
    else:
        output_matches = process.stdout.strip() == sentinel
    if process.returncode != 73 or not output_matches:
        raise StagedValidationError(
            f"{system} network-denial probe did not return the expected sentinel: "
            + diagnostic.strip()
        )
    return network_isolation


def _offline_environment(*, cli_config: Path, data_dir: Path) -> dict[str, str]:
    environment = os.environ.copy()
    proxy_names = {"http_proxy", "https_proxy", "all_proxy", "no_proxy"}
    for key in tuple(environment):
        if key.lower() in proxy_names:
            del environment[key]
    environment.update(
        {
            "ALL_PROXY": "http://127.0.0.1:1",
            "CHECKPOINT_DISABLE": "1",
            "HTTP_PROXY": "http://127.0.0.1:1",
            "HTTPS_PROXY": "http://127.0.0.1:1",
            "NO_PROXY": "",
            "TF_CLI_CONFIG_FILE": str(cli_config),
            "TF_DATA_DIR": str(data_dir),
            "TF_IN_AUTOMATION": "1",
            "all_proxy": "http://127.0.0.1:1",
            "http_proxy": "http://127.0.0.1:1",
            "https_proxy": "http://127.0.0.1:1",
            "no_proxy": "",
        }
    )
    return environment


def validate_staged_module(
    workdir: Path,
    candidates: Mapping[Path, bytes],
    *,
    tofu_binary: str = "tofu",
) -> StagedValidationResult:
    """Validate candidate root files using only copied, initialized dependencies."""
    workdir = _normalized_absolute(workdir)
    if not workdir.is_dir():
        raise StagedValidationError(f"module workdir does not exist: {workdir}")
    root_inputs = _root_inputs(workdir, candidates)

    with tempfile.TemporaryDirectory(prefix="ubitofu-staged-validation-") as temporary:
        private_root = Path(temporary).resolve()
        private_root.chmod(0o700)
        stage_fs = private_root / "fs"
        stage_root = stage_fs / workdir.relative_to(workdir.anchor)
        stage_root.mkdir(parents=True, mode=0o700)
        for name, content in root_inputs.items():
            (stage_root / name).write_bytes(content)

        (
            local_modules,
            registry_modules,
            required_providers,
            module_cache_paths,
            module_cache_files,
        ) = _stage_modules(
            workdir=workdir,
            stage_root=stage_root,
            stage_fs=stage_fs,
            root_inputs=root_inputs,
        )
        providers, provider_cache_paths = _copy_provider_cache(
            workdir, stage_root, required_providers
        )

        empty_mirror = private_root / "empty-provider-mirror"
        empty_mirror.mkdir(mode=0o700)
        cli_config = private_root / "tofurc"
        cli_config.write_text(
            "disable_checkpoint = true\n"
            "provider_installation {\n"
            "  filesystem_mirror {\n"
            f'    path = "{empty_mirror}"\n'
            '    include = ["*/*"]\n'
            "  }\n"
            "}\n",
            encoding="utf-8",
        )
        environment = _offline_environment(
            cli_config=cli_config, data_dir=stage_root / ".terraform"
        )
        network_isolation = verify_offline_enforcement()
        sandbox_prefix = _sandbox_prefix()
        process = subprocess.run(
            [*sandbox_prefix, tofu_binary, "validate", "-no-color"],
            cwd=stage_root,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
            timeout=60,
        )
        if process.returncode != 0:
            diagnostic = (process.stderr or process.stdout).strip()
            if "init" in diagnostic.lower() or "not installed" in diagnostic.lower():
                raise StagedValidationError(
                    f"dependencies unavailable; initialize first: {diagnostic}"
                )
            raise StagedValidationError(f"staged tofu validate failed: {diagnostic}")

        version = subprocess.run(
            [*sandbox_prefix, tofu_binary, "version"],
            env=environment,
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        ).stdout.strip()
        return StagedValidationResult(
            tofu_version=version,
            root_files=tuple(sorted(root_inputs)),
            local_modules=local_modules,
            registry_modules=registry_modules,
            providers=providers,
            cache_paths=tuple(sorted((*module_cache_paths, *provider_cache_paths))),
            module_cache_files=module_cache_files,
            network_isolation=network_isolation,
            stdout=process.stdout,
        )
