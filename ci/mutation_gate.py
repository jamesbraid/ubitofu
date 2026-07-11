#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 James Braid
"""Enforced mutation testing gate for ubitofu CI.

Two layers:

  Layer 1 (pr):
    Detects which correctness-critical modules changed in the PR vs. origin/main,
    scopes mutmut to only those modules, and fails if any mutant survives.
    Fast: typically 1-3 minutes for one or two small modules.

  Layer 2 (sweep):
    Mutation-tests all four modules and fails if the score drops below the
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
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent

# Modules the per-PR gate (Layer 1) enforces to zero surviving mutants.
# pipeline.py is intentionally EXCLUDED for now: its ~245 remaining survivors are
# a tracked backlog (only run_generate is cleaned), so gating it per-PR would
# block every pipeline change. The weekly sweep (Layer 2) still covers pipeline.py
# via pyproject `only_mutate`, so overall erosion is caught. Re-add pipeline.py
# here once its backlog reaches zero.
MODULES = [
    "src/ubitofu/enumerator.py",
    "src/ubitofu/import_emitter.py",
    "src/ubitofu/hcl_surgeon.py",
]


def detect_changed_modules() -> list[str]:
    """Return the subset of MODULES that appear in the PR diff vs. origin/main.

    Falls back to all modules if the diff cannot be computed (e.g. shallow clone
    without a fetched origin/main), so the gate still runs and is still enforced.
    """
    result = subprocess.run(
        ["git", "diff", "--name-only", "origin/main...HEAD"],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
    )
    if result.returncode != 0:
        print(
            "WARNING: could not compute diff vs. origin/main; "
            "running on all modules as fallback.",
            file=sys.stderr,
        )
        return list(MODULES)
    changed = set(result.stdout.splitlines())
    matched = [m for m in MODULES if m in changed]
    return matched if matched else []


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


def run_mutmut() -> int:
    """Run mutmut; return the exit code."""
    result = subprocess.run(
        [sys.executable, "-m", "mutmut", "run"],
        cwd=REPO_ROOT,
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

    rc = run_mutmut()
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

    if survived > 0:
        sys.exit(
            f"FAIL: {survived} mutant(s) survived in changed code — "
            "add tests or annotate with '# pragma: no mutate — <reason>'"
        )

    print("PASS: all mutants killed")


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
    parser.add_argument("mode", choices=["pr", "sweep"])
    parser.add_argument(
        "--threshold",
        type=int,
        default=80,
        help="Minimum mutation score %% for sweep mode (default: 80)",
    )
    args = parser.parse_args()

    pyproject = REPO_ROOT / "pyproject.toml"

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
