"""Development-only dependency floors remain importable."""

from __future__ import annotations

import os
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest
import yaml
from packaging.requirements import Requirement


def _require_unscoped_mutation_config() -> None:
    if os.environ.get("UBITOFU_MUTATION_SCOPED") == "1":
        pytest.skip("PR mutation worker intentionally narrows only_mutate")


def _woodpecker_step(document: str, name: str) -> str:
    marker = f"  - name: {name}\n"
    start = document.index(marker)
    end = document.find("\n  - name: ", start + len(marker))
    return document[start:] if end == -1 else document[start:end]


def test_hypothesis_available_for_property_tests():
    import hypothesis  # noqa: F401
    from hypothesis import given  # noqa: F401


def test_pytest_cov_plugin_installed():
    import pytest_cov  # noqa: F401


def test_no_isolation_package_test_declares_its_build_toolchain() -> None:
    repository = Path(__file__).resolve().parents[1]
    with (repository / "pyproject.toml").open("rb") as source:
        document = tomllib.load(source)

    dev = {
        Requirement(value).name
        for value in document["project"]["optional-dependencies"]["dev"]
    }
    backend = {Requirement(value).name for value in document["build-system"]["requires"]}

    assert {"build", *backend} <= dev


def test_controller_extra_requires_testcontainers_tmpfs_api_floor() -> None:
    repository = Path(__file__).resolve().parents[1]
    with (repository / "pyproject.toml").open("rb") as source:
        document = tomllib.load(source)

    controller = [
        Requirement(value)
        for value in document["project"]["optional-dependencies"]["controller"]
    ]
    testcontainers = next(
        requirement for requirement in controller if requirement.name == "testcontainers"
    )

    assert testcontainers.specifier.contains("4.15")
    assert not testcontainers.specifier.contains("4.14.999")


@pytest.mark.parametrize(
    ("machine", "archive_arch"),
    [("x86_64", "amd64"), ("amd64", "amd64"), ("aarch64", "arm64"), ("arm64", "arm64")],
)
def test_tofu_installer_selects_the_native_linux_archive(
    machine: str, archive_arch: str
) -> None:
    repository = Path(__file__).resolve().parents[1]
    script = repository / "ci" / "install-tofu.sh"

    result = subprocess.run(
        [
            "bash",
            "-c",
            'source "$1"; tofu_archive_url "$2"',
            "ubitofu-test",
            str(script),
            machine,
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    assert result.stdout == (
        "https://github.com/opentofu/opentofu/releases/download/v1.12.0/"
        f"tofu_1.12.0_linux_{archive_arch}.zip"
    )


def test_tofu_installer_rejects_an_unsupported_architecture() -> None:
    repository = Path(__file__).resolve().parents[1]
    script = repository / "ci" / "install-tofu.sh"

    result = subprocess.run(
        [
            "bash",
            "-c",
            'source "$1"; tofu_archive_url "$2"',
            "ubitofu-test",
            str(script),
            "synthetic-unsupported",
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert result.stdout == ""
    assert result.stderr == "unsupported Linux architecture: synthetic-unsupported\n"


def test_mutation_configuration_is_exactly_consistent() -> None:
    _require_unscoped_mutation_config()
    from ci.mutation_gate import configured_modules, woodpecker_modules

    repository = Path(__file__).resolve().parents[1]
    configured = configured_modules(repository / "pyproject.toml")
    filtered = woodpecker_modules(repository / ".woodpecker" / "ci.yml")

    assert configured
    assert configured == tuple(sorted(set(configured)))
    assert configured == tuple(sorted(set(filtered)))


def test_full_proof_workflows_are_serialized_without_duplicate_sweeps() -> None:
    repository = Path(__file__).resolve().parents[1]
    workflows = repository / ".woodpecker"
    ci = (workflows / "ci.yml").read_text()
    controller = (workflows / "controller.yml").read_text()
    mutation = (workflows / "mutation.yml").read_text()
    sweep = "python ci/mutation_gate.py sweep --threshold 80"

    assert "depends_on:\n  - ci\n" in controller
    assert "depends_on:\n  - controller\n" in mutation
    assert yaml.safe_load(controller)["when"] == [
        {"event": ["pull_request", "tag", "manual", "cron"]}
    ]
    assert yaml.safe_load(mutation)["when"] == [
        {"event": ["cron", "manual"]}
    ]
    assert "  - name: mutation-sweep\n" not in ci
    assert (
        sum(document.count(sweep) for document in (ci, controller, mutation)) == 1
    )
    assert "event: [push, pull_request, tag, manual, cron]" in _woodpecker_step(
        ci, "test"
    )
    assert "event: [push, pull_request, tag, manual, cron]" in _woodpecker_step(
        ci, "gitleaks"
    )
    assert "when:" not in _woodpecker_step(controller, "controller-tests")
    assert "when:" not in _woodpecker_step(mutation, "mutation-sweep")


def test_full_proof_timeout_and_cli_trigger_are_documented() -> None:
    repository = Path(__file__).resolve().parents[1]
    testing = (repository / "docs" / "testing.md").read_text()

    assert "repo update --timeout 2h <owner/repo>" in testing
    assert "pipeline create --branch <candidate-branch> <owner/repo>" in testing
    assert "ci -> controller -> mutation" in testing


def test_mutation_worker_copies_gate_inputs() -> None:
    repository = Path(__file__).resolve().parents[1]
    with (repository / "pyproject.toml").open("rb") as source:
        also_copy = tomllib.load(source)["tool"]["mutmut"]["also_copy"]

    assert set(also_copy) >= {
        ".woodpecker",
        ".github",
        "ci",
        "docs/testing.md",
    }


def test_mutation_worker_skips_distribution_boundary_test() -> None:
    repository = Path(__file__).resolve().parents[1]
    with (repository / "pyproject.toml").open("rb") as source:
        selection = tomllib.load(source)["tool"]["mutmut"][
            "pytest_add_cli_args_test_selection"
        ]

    assert selection == ["--ignore=tests/test_packaging.py"]


def test_mutmut_stats_accepts_synthetic_import_frames() -> None:
    repository = Path(__file__).resolve().parents[1]
    environment = os.environ.copy()
    environment.pop("MUTANT_UNDER_TEST", None)
    probe = """
import mutmut
from mutmut.__main__ import record_trampoline_hit

name = "ubitofu.synthetic.probe"
code = compile(
    "record_trampoline_hit(name)",
    "<frozen importlib._bootstrap>",
    "exec",
)
exec(code, {"name": name, "record_trampoline_hit": record_trampoline_hit})
assert name in mutmut._stats
"""

    result = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=repository,
        env=environment,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr


def test_mutation_check_is_non_mutating(tmp_path) -> None:
    _require_unscoped_mutation_config()
    repository = Path(__file__).resolve().parents[1]
    pyproject = repository / "pyproject.toml"
    woodpecker = repository / ".woodpecker" / "ci.yml"
    before = (pyproject.read_bytes(), woodpecker.read_bytes())
    result = subprocess.run(
        [sys.executable, "ci/mutation_gate.py", "check"],
        cwd=repository,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert (pyproject.read_bytes(), woodpecker.read_bytes()) == before
