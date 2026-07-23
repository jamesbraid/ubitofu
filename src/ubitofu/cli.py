# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 James Braid
import argparse
import subprocess
import sys
from pathlib import Path
from typing import IO

import httpx

from .config import Config, ConfigError, load_config, validate_config
from .controller import Controller, controller_from_config
from .coverage import audit
from .enumerator import enumerate_controller
from .import_emitter import emit_import_blocks
from .reporter import format_coverage
from .tofu_runner import TofuError, TofuRunner

# One scheme for every subcommand, rsync-style: a flat enumeration of distinct
# small codes (case-friendly in shell), errors at the conventional low values.
_EXIT_EPILOG = (
    "exit codes (same scheme for every subcommand):\n"
    "  0    success — in sync / clean plan / nothing to report\n"
    "  10   drift captured — committed *.tf edited or reconciled_new.tf\n"
    "       appended (reconcile)\n"
    "  11   attention required — complex/diverged/orphaned/secret findings\n"
    "       (reconcile), real drift (verify)\n"
    "  12   drift captured AND attention required\n"
    "  13   forbidden device create — remove the block or adopt via UI\n"
    "       (reconcile)\n"
    "  1    error — controller unreachable, tofu failure, secrets\n"
    "  2    usage error — bad invocation or config\n"
    'shell: case "$rc" in 10) pr;; 11) notify;; 12) pr; notify;; 13) fail;; esac\n'
)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="ubitofu",
        description="Plan-only UniFi -> OpenTofu importer.",
    )
    sub = p.add_subparsers(dest="command", required=True)
    for name in ("enumerate", "generate", "reconcile", "verify"):
        sp = sub.add_parser(
            name,
            epilog=_EXIT_EPILOG,
            formatter_class=argparse.RawDescriptionHelpFormatter,
        )
        sp.add_argument("--config", required=True)
        sp.add_argument("--controller-url")
        sp.add_argument("--site")
        sp.add_argument("--api-key-source", choices=["op", "env"])
        if name in ("enumerate", "generate"):
            sp.add_argument("--mode", choices=["bulk", "incremental"], default="bulk")
        if name == "reconcile":
            sp.add_argument("--check", action="store_true",
                            help="classify and report, write nothing; exit "
                                 "codes as a wet run (the apply gate)")
    return p


def _controller(cfg: Config) -> Controller:
    return controller_from_config(cfg)


def cmd_enumerate(cfg: Config, mode: str, out: IO[str]) -> int:
    ctl = _controller(cfg)
    try:
        runner = TofuRunner(workdir=Path(cfg.workdir))
        try:
            schema = runner.providers_schema()
        except TofuError as exc:
            raise TofuError(
                f"{exc}\nenumerate needs the provider schema for the coverage "
                f"audit: run `tofu init` in {cfg.workdir}") from exc
        res = enumerate_controller(ctl)
        report = audit(ctl, schema)
        print(emit_import_blocks(res.targets), file=out)
        print(format_coverage(res.gaps + report.gap_lines(),
                              len(report.accepted)), file=out)
        return 0
    finally:
        ctl.close()


def cmd_generate(cfg: Config, mode: str, out: IO[str]) -> int:
    # pipeline.run_generate wires Tasks 1-9 end-to-end (implemented in Task 12).
    # Lazy import so cli is importable before Task 12 exists.
    from .pipeline import run_generate  # noqa: PLC0415

    return run_generate(cfg, mode, out)


def cmd_reconcile(cfg: Config, out: IO[str], check: bool = False) -> int:
    from .pipeline import run_reconcile  # noqa: PLC0415

    return run_reconcile(cfg, out, check=check)


def cmd_verify(cfg: Config, out: IO[str]) -> int:
    from .pipeline import run_verify  # noqa: PLC0415

    return run_verify(cfg, out)


def _cannot_reach(cfg: Config, exc: Exception) -> int:
    print(
        f"ubitofu: cannot reach the UniFi controller ({cfg.controller_url}): {exc}",
        file=sys.stderr,
    )
    return 1


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        # Validation is deferred until after the flag overrides below, so a
        # flag can rescue an incomplete config file (--api-key-source env
        # filling in for a missing api_key_source) and, conversely, a flag
        # that invalidates an otherwise-valid config (--api-key-source op
        # with no op_vault) still gets caught instead of bypassing
        # validation entirely.
        cfg = load_config(args.config, validate=False)
        # CLI flags override config-file values.
        if args.controller_url:
            cfg.controller_url = args.controller_url
        if args.site:
            cfg.site = args.site
        if args.api_key_source:
            cfg.api_key_source = args.api_key_source
        validate_config(cfg)
    except ConfigError as exc:
        print(f"ubitofu: config error: {exc}", file=sys.stderr)
        return 2
    try:
        if args.command == "enumerate":
            return cmd_enumerate(cfg, args.mode, sys.stdout)
        if args.command == "generate":
            return cmd_generate(cfg, args.mode, sys.stdout)
        if args.command == "reconcile":
            return cmd_reconcile(cfg, sys.stdout, check=getattr(args, "check", False))
        return cmd_verify(cfg, sys.stdout)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code in (401, 403):
            print(
                f"ubitofu: authentication failed for {cfg.controller_url}"
                " — check username/password or API key",
                file=sys.stderr,
            )
            return 1
        return _cannot_reach(cfg, exc)
    except httpx.HTTPError as exc:
        return _cannot_reach(cfg, exc)
    except TofuError as exc:
        print(f"ubitofu: tofu failed: {exc}", file=sys.stderr)
        return 1
    except subprocess.CalledProcessError:
        print(
            "ubitofu: 1Password not signed in or key missing"
            " — run 'op signin' / set the api-key source",
            file=sys.stderr,
        )
        return 1
    except Exception as exc:  # noqa: BLE001
        print(
            f"ubitofu: unexpected error: {type(exc).__name__}: {exc} (please report)",
            file=sys.stderr,
        )
        return 1
