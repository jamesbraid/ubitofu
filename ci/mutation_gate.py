#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 James Braid
"""Enforced mutation testing gate for ubitofu CI.

Two layers:

  Layer 1 (pr):
    Detects which correctness-critical modules changed in the PR, runs mutmut
    on those modules, and fails if a mutant on a line the PR changed survives.
    Survivors elsewhere in the module are reported, not gated: they are the
    sweep's business. A survivor whose line cannot be located counts as in
    scope, so the gate never under-gates. Without a reachable target branch
    the gate falls back to the whole module.
    Fast: typically 1-3 minutes for one or two small modules.

  Layer 1 report (pr-report):
    The line-scoped verdict over an existing mutants/ directory, without
    running mutmut again. For local use after `pr`.

  Layer 2 (sweep):
    Mutation-tests all configured correctness modules and fails if the score drops below the
    given threshold.  Intended for weekly cron + manual runs as a backstop
    against slow test erosion.
    Add the weekly cron with:
      woodpecker-cli cron add --repo <owner/repo> \\
        --name mutation-weekly --expr "0 2 * * 1" --branch main

Exit codes:
  0  gate passed
  1  gate failed or misconfiguration (message printed to stderr)

Equivalent-mutant suppression:
  Lines with known equivalent mutations may be annotated:
    result = x or y  # pragma: no mutate — equivalent: short-circuit irrelevant for callers
  Block suppression:
    # pragma: no mutate start
    ...
    # pragma: no mutate end
  Every pragma must carry an inline explanatory comment after the em-dash.
"""
from __future__ import annotations

import argparse
import ast
import json
import os
import re
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent

MODULES = (
    "src/ubitofu/cleaner.py",
    "src/ubitofu/cli.py",
    "src/ubitofu/config.py",
    "src/ubitofu/controller.py",
    "src/ubitofu/controller_projection.py",
    "src/ubitofu/coverage.py",
    "src/ubitofu/enumerator.py",
    "src/ubitofu/errors.py",
    "src/ubitofu/file_metadata.py",
    "src/ubitofu/file_transaction.py",
    "src/ubitofu/generate.py",
    "src/ubitofu/hcl_index.py",
    "src/ubitofu/hcl_patches.py",
    "src/ubitofu/hcl_writer.py",
    "src/ubitofu/health.py",
    "src/ubitofu/import_emitter.py",
    "src/ubitofu/inspect.py",
    "src/ubitofu/manifest.py",
    "src/ubitofu/module_index.py",
    "src/ubitofu/outcomes.py",
    "src/ubitofu/pipeline.py",
    "src/ubitofu/plan_check.py",
    "src/ubitofu/reconcile_model.py",
    "src/ubitofu/reconcile_planner.py",
    "src/ubitofu/reconcile_renderer.py",
    "src/ubitofu/reconcile_snapshot.py",
    "src/ubitofu/runtime.py",
    "src/ubitofu/secrets.py",
    "src/ubitofu/tofu_json.py",
    "src/ubitofu/tofu_runner.py",
    "src/ubitofu/values.py",
)


_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
_SURVIVOR = re.compile(r"^\s*(\S+): survived\s*$")
_MUTANT_NAME = re.compile(r"^(?P<module>.+)\.(?P<function>x_.+?)__mutmut_(?P<index>\d+)$")


def changed_lines_from_diff(diff: str) -> dict[str, set[int]]:
    """Map each file in a unified diff to the new-side line numbers it adds or replaces."""
    changed: dict[str, set[int]] = {}
    current: str | None = None
    for line in diff.splitlines():
        if line.startswith("+++ "):
            path = line[4:].strip()
            if path.startswith("b/"):
                path = path[2:]
            current = None if path == "/dev/null" else path
            if current is not None:
                changed.setdefault(current, set())
            continue
        match = _HUNK.match(line)
        if match is None or current is None:
            continue
        start = int(match.group(3))
        count = 1 if match.group(4) is None else int(match.group(4))
        changed[current].update(range(start, start + count))
    return changed


def survivors_from_results(results: str) -> list[str]:
    """Names of the mutants `mutmut results` lists as survived."""
    names: list[str] = []
    for line in results.splitlines():
        match = _SURVIVOR.match(line)
        if match is not None:
            names.append(match.group(1))
    return names


def mutant_location(name: str, show: str, source: str) -> tuple[str, int] | None:
    """Locate a mutant's changed line in its source file.

    `mutmut show` prints a diff of the mutated function with line numbers
    relative to the function's `def` line. The function's own line comes from
    the source file's AST, so the two combine into a file line. None means
    the function could not be found, which the caller treats as in scope.
    """
    named = _MUTANT_NAME.match(name)
    if named is None:
        return None
    function = named.group("function")[2:]  # strip mutmut's "x_" prefix
    path: str | None = None
    for line in show.splitlines():
        if line.startswith("--- "):
            path = line[4:].strip()
            break
    if path is None:
        return None
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None
    def_line: int | None = None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name == function:
            def_line = node.lineno
            break
    if def_line is None:
        return None
    relative: int | None = None
    in_hunk = False
    old_cursor = 0
    for line in show.splitlines():
        header = _HUNK.match(line)
        if header is not None:
            in_hunk = True
            old_cursor = int(header.group(1))
            continue
        if not in_hunk:
            continue
        if line.startswith("-"):
            relative = old_cursor
            break
        if line.startswith("+"):
            relative = max(old_cursor - 1, 1)
            break
        old_cursor += 1
    if relative is None:
        return None
    return path, def_line + relative - 1


def scoped_survivors(
    located: dict[str, tuple[str, int] | None], changed: dict[str, set[int]]
) -> dict[str, tuple[str, int] | None]:
    """Survivors on a changed line, plus any survivor that could not be located."""
    return {
        name: location
        for name, location in located.items()
        if location is None or location[1] in changed.get(location[0], set())
    }


def _git(token: str | None, *args: str) -> list[str]:
    """A git command that can authenticate to the forge when a token is given.

    Pull-request workspaces carry no forge credential, so when the pipeline
    provides FORGEJO_TOKEN a credential helper hands it to git from the
    environment. The token never appears on the command line.
    """
    if token is None:
        return ["git", *args]
    helper = "!f() { echo username=oauth2; echo \"password=$FORGEJO_TOKEN\"; }; f"
    return ["git", "-c", f"credential.helper={helper}", *args]


def fetch_target_command(target: str, token: str | None) -> list[str]:
    """Fetch the target branch tip, with its whole tree, into FETCH_HEAD.

    The PR checkout is a tree-filtered partial clone. Fetching the target the
    same way would leave `git diff` to fetch objects lazily through a remote
    that has no credential, so the target's tree comes down in full.
    """
    return _git(token, "fetch", "--quiet", "--depth=1", "origin", target)


def diff_command(modules: list[str], base: str, token: str | None) -> list[str]:
    return _git(token, "diff", "-U0", base, "HEAD", "--", *modules)


def pr_base_ref() -> str | None:
    """The ref to diff the PR against, fetched if Woodpecker names it."""
    target = os.environ.get("CI_COMMIT_TARGET_BRANCH")
    if target:
        fetched = subprocess.run(
            fetch_target_command(target, os.environ.get("FORGEJO_TOKEN") or None),
            cwd=REPO_ROOT,
        )
        return "FETCH_HEAD" if fetched.returncode == 0 else None
    probe = subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", "origin/main"],
        cwd=REPO_ROOT,
        capture_output=True,
    )
    return "origin/main" if probe.returncode == 0 else None


def changed_lines(modules: list[str], base: str) -> dict[str, set[int]]:
    result = subprocess.run(
        diff_command(modules, base, os.environ.get("FORGEJO_TOKEN") or None),
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        sys.exit(f"ERROR: git diff against {base} failed: {result.stderr.strip()}")
    return changed_lines_from_diff(result.stdout)


def locate_survivors(names: list[str]) -> dict[str, tuple[str, int] | None]:
    sources: dict[str, str] = {}
    located: dict[str, tuple[str, int] | None] = {}
    for name in names:
        show = subprocess.run(
            [sys.executable, "-m", "mutmut", "show", name],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
        ).stdout
        path = next(
            (line[4:].strip() for line in show.splitlines() if line.startswith("--- ")),
            None,
        )
        if path is None:
            located[name] = None
            continue
        if path not in sources:
            try:
                sources[path] = (REPO_ROOT / path).read_text()
            except OSError:
                sources[path] = ""
        located[name] = mutant_location(name, show, sources[path])
    return located


def line_scoped_verdict(changed_modules: list[str]) -> tuple[int, int]:
    """Print the survivors that matter and return (in scope, module-wide)."""
    results = subprocess.run(
        [sys.executable, "-m", "mutmut", "results"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    ).stdout
    names = survivors_from_results(results)
    base = pr_base_ref()
    if base is None:
        print(
            "No target branch to diff against; every survivor in the changed "
            "modules counts.",
            file=sys.stderr,
        )
        for name in names:
            print(f"  survived: {name}")
        return len(names), len(names)
    changed = changed_lines(changed_modules, base)
    scoped = scoped_survivors(locate_survivors(names), changed)
    for name, location in sorted(scoped.items()):
        where = "unlocated" if location is None else f"{location[0]}:{location[1]}"
        print(f"  survived on a changed line: {where} {name}")
    return len(scoped), len(names)


def configured_modules(pyproject_path: Path) -> tuple[str, ...]:
    """Read the checked-in mutation scope without changing it."""
    with pyproject_path.open("rb") as source:
        document = tomllib.load(source)
    configured = document["tool"]["mutmut"]["only_mutate"]
    if not isinstance(configured, list) or not all(
        isinstance(item, str) for item in configured
    ):
        raise ValueError("invalid pyproject mutation scope")
    return tuple(configured)


def woodpecker_modules(path: Path) -> tuple[str, ...]:
    """Read the mutation-pr path filter from the dependency-free CI YAML subset."""
    lines = path.read_text().splitlines()
    try:
        start = next(
            index for index, line in enumerate(lines) if line == "  - name: mutation-pr"
        )
    except StopIteration as exc:
        raise ValueError("mutation-pr step is missing") from exc
    end = next(
        (
            index
            for index in range(start + 1, len(lines))
            if lines[index].startswith("  - name: ")
        ),
        len(lines),
    )
    in_include = False
    modules: list[str] = []
    for line in lines[start:end]:
        if line == "        include:":
            in_include = True
            continue
        if in_include and line.startswith("          - "):
            modules.append(line.removeprefix("          - "))
        elif in_include and line.strip() and not line.startswith("          "):
            in_include = False
    if not modules:
        raise ValueError("mutation-pr path filter is empty")
    return tuple(modules)


def check_configuration(pyproject: Path, woodpecker: Path) -> None:
    """Fail unless every mutation-scope declaration is the same sorted set."""
    declared = configured_modules(pyproject)
    filtered = woodpecker_modules(woodpecker)
    expected = tuple(sorted(set(MODULES)))
    if tuple(MODULES) != expected:
        raise ValueError("MODULES must be sorted and unique")
    if declared != expected:
        raise ValueError("pyproject mutation scope differs from MODULES")
    if tuple(sorted(set(filtered))) != expected or len(filtered) != len(expected):
        raise ValueError("Woodpecker mutation filters differ from MODULES")


def detect_changed_modules() -> list[str]:
    """Return the subset of MODULES that changed in this push/PR.

    Uses Woodpecker's native CI_PIPELINE_FILES (a JSON array of the changed
    files) — no git fetch or diff. Falls back to all modules when the variable
    is unset or unparseable (local runs, or Woodpecker's >500-file cap), so the
    gate still runs and never under-gates.
    """
    raw = os.environ.get("CI_PIPELINE_FILES")
    if not raw:
        print(
            "CI_PIPELINE_FILES unset; running on all modules as fallback.",
            file=sys.stderr,
        )
        return list(MODULES)
    try:
        changed = set(json.loads(raw))
    except (ValueError, TypeError):
        print(
            "CI_PIPELINE_FILES is not valid JSON; running on all modules "
            "as fallback.",
            file=sys.stderr,
        )
        return list(MODULES)
    return [m for m in MODULES if m in changed]


def patch_only_mutate(pyproject_path: Path, modules: list[str]) -> None:
    """Replace only_mutate in pyproject.toml to scope mutmut to *modules*.

    The workspace is ephemeral in CI so we patch in place; no restore needed.
    """
    text = pyproject_path.read_text()
    new_list = "[\n" + "".join(f'    "{m}",\n' for m in modules) + "]"
    patched, count = re.subn(
        r"^only_mutate\s*=\s*\[.*?\]",
        f"only_mutate = {new_list}",
        text,
        flags=re.MULTILINE | re.DOTALL,
    )
    if count == 0:
        sys.exit("ERROR: could not locate only_mutate key in pyproject.toml")
    pyproject_path.write_text(patched)


def run_mutmut(*, scoped: bool = False) -> int:
    """Run mutmut; return the exit code."""
    environment = os.environ.copy()
    if scoped:
        # The PR gate intentionally narrows only_mutate after the exact full
        # configuration has passed. Infrastructure tests use this marker to
        # avoid mistaking that worker-local narrowing for checked-in drift.
        environment["UBITOFU_MUTATION_SCOPED"] = "1"
    result = subprocess.run(
        [sys.executable, "-m", "mutmut", "run"],
        cwd=REPO_ROOT,
        env=environment,
    )
    return result.returncode


def export_stats() -> dict[str, int]:
    """Run export-cicd-stats and return the parsed JSON dict."""
    subprocess.run(
        [sys.executable, "-m", "mutmut", "export-cicd-stats"],
        cwd=REPO_ROOT,
        check=True,
    )
    stats_path = REPO_ROOT / "mutants" / "mutmut-cicd-stats.json"
    return json.loads(stats_path.read_text())  # type: ignore[no-any-return]


def gate_pr(pyproject: Path) -> None:
    """Layer 1: diff-scoped gate.  Fail if any mutant on changed code survives."""
    changed = detect_changed_modules()
    if not changed:
        print("No correctness-critical modules changed; skipping mutation gate.")
        return

    print(f"Changed modules: {changed}")
    patch_only_mutate(pyproject, changed)

    rc = run_mutmut(scoped=True)
    if rc != 0:
        sys.exit(f"ERROR: mutmut run exited {rc} — check above for crash details")

    stats = export_stats()
    killed = stats["killed"]
    survived = stats["survived"]
    total = stats["total"]

    if total == 0:
        sys.exit(
            "ERROR: zero mutants generated — possible crash or misconfiguration "
            "(check that the changed modules are covered by tests)"
        )

    print(f"Mutants: {total} total, {killed} killed, {survived} survived, "
          f"{stats['timeout']} timeout")
    report_pr_verdict(changed)


def report_pr_verdict(changed: list[str]) -> None:
    """Fail on a survivor in a line the PR changed; report the rest."""
    in_scope, module_wide = line_scoped_verdict(changed)
    print(f"Survivors on changed lines: {in_scope} (module-wide: {module_wide})")
    if in_scope > 0:
        sys.exit(
            f"FAIL: {in_scope} mutant(s) survived on lines this change touched — "
            "add tests or annotate with '# pragma: no mutate — <reason>'"
        )
    print("PASS: no mutant survived on a changed line")


def gate_sweep(pyproject: Path, threshold: int) -> None:
    """Layer 2: full sweep.  Fail if score drops below threshold."""
    rc = run_mutmut()
    if rc != 0:
        sys.exit(f"ERROR: mutmut run exited {rc} — check above for crash details")

    stats = export_stats()
    killed = stats["killed"]
    survived = stats["survived"]
    total = stats["total"]

    if total == 0:
        sys.exit(
            "ERROR: zero mutants generated — possible crash or misconfiguration"
        )

    denominator = killed + survived
    if denominator == 0:
        sys.exit(
            "ERROR: all mutants timed out or were skipped — cannot compute score"
        )

    score = killed / denominator * 100
    print(
        f"Mutants: {total} total, {killed} killed, {survived} survived, "
        f"{stats['timeout']} timeout"
    )
    print(f"Mutation score: {score:.1f}% (threshold: {threshold}%)")

    if score < threshold:
        sys.exit(
            f"FAIL: mutation score {score:.1f}% is below threshold {threshold}% — "
            "add tests or annotate documented equivalents with '# pragma: no mutate — <reason>'"
        )

    print("PASS: mutation score above threshold")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["check", "pr", "pr-report", "sweep"])
    parser.add_argument(
        "--threshold",
        type=int,
        default=80,
        help="Minimum mutation score %% for sweep mode (default: 80)",
    )
    args = parser.parse_args()

    pyproject = REPO_ROOT / "pyproject.toml"

    try:
        check_configuration(pyproject, REPO_ROOT / ".woodpecker" / "ci.yml")
    except (KeyError, OSError, TypeError, ValueError) as exc:
        sys.exit(f"ERROR: {exc}")
    if args.mode == "check":
        print("PASS: mutation configuration is consistent")
        return

    if args.mode == "pr-report":
        report_pr_verdict(detect_changed_modules())
        return

    # Clean stale mutmut state so partial results from previous runs don't pollute.
    mutants_dir = REPO_ROOT / "mutants"
    if mutants_dir.exists():
        shutil.rmtree(mutants_dir)

    if args.mode == "pr":
        gate_pr(pyproject)
    else:
        gate_sweep(pyproject, args.threshold)


if __name__ == "__main__":
    main()
