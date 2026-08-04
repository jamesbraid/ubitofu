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
from .errors import TofuExecutionError
from .import_emitter import emit_import_blocks
from .provider_contract import (
    ContractError,
    ContractExecution,
    resolve_configured_execution,
)
from .reporter import format_coverage
from .tofu_runner import TofuError, TofuRunner

# Failure codes: the run could not complete, so it has no finding to report.
# They occupy their own band so the first digit says which kind of answer you
# got — 1x is an outcome the run reached, 2x is a reason it never got there.
# A caller that only cares "did it work" still tests non-zero.
EXIT_CONTROLLER_UNREACHABLE = 20  # transport failure, or a non-auth error response
EXIT_AUTH_FAILED = 21             # controller rejected the credentials (401/403)
EXIT_SECRET_UNAVAILABLE = 22      # `op read` failed — not signed in, or no such item
EXIT_TOFU_FAILED = 23             # tofu itself failed: init, plan, schema, fmt

# One scheme for every subcommand, rsync-style: a flat enumeration of distinct
# small codes (case-friendly in shell), errors at the conventional low values.
_EXIT_EPILOG = (
    "exit codes (same scheme for every subcommand):\n"
    "  0    success — in sync / clean plan / nothing to report\n"
    "  10   drift captured — config edited, import emitted, or object appended\n"
    "       (reconcile)\n"
    "  11   attention required — complex drift, existence ambiguity,\n"
    "       replacement, invariant, or secret findings (reconcile); real drift\n"
    "       (verify); schema findings (migrate)\n"
    "  12   drift captured AND attention required\n"
    "  13   forbidden device create — remove the block or adopt via UI\n"
    "       (reconcile)\n"
    "  20   cannot reach or use the controller — retry is reasonable\n"
    "  21   authentication failed — fix the credentials; retrying will not help\n"
    "  22   secret unavailable — `op signin`, or check api_key_ref\n"
    "  23   tofu failed — after a provider bump, try `ubitofu migrate`\n"
    "  1    unexpected error — please report\n"
    "  2    usage error — bad invocation or config\n"
    'shell: case "$rc" in 10) pr;; 11) notify;; 12) pr; notify;; 13) fail;; esac\n'
)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="ubitofu",
        description="Plan-only UniFi -> OpenTofu importer.",
    )
    sub = p.add_subparsers(dest="command", required=True)
    for name in ("enumerate", "generate", "reconcile", "verify", "migrate"):
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
        if name == "migrate":
            sp.add_argument("--write-baseline", action="store_true",
                            help="record the installed provider's schema as the "
                                 "baseline to diff the next bump against")
    return p


def _controller(cfg: Config) -> Controller:
    return controller_from_config(cfg)


def cmd_enumerate(
    cfg: Config,
    mode: str,
    out: IO[str],
    *,
    execution: ContractExecution | None = None,
) -> int:
    ctl = _controller(cfg)
    try:
        runner = execution.runner if execution is not None else TofuRunner(
            workdir=Path(cfg.workdir)
        )
        try:
            schema = (
                execution.schema
                if execution is not None and execution.schema is not None
                else runner.providers_schema()
            )
        except TofuError as exc:
            raise TofuExecutionError(
                "providers", 1, "run tofu init before retrying"
            ) from exc
        res = enumerate_controller(ctl)
        report = audit(ctl, schema)
        print(emit_import_blocks(res.targets), file=out)
        print(format_coverage(res.gaps + report.gap_lines(),
                              len(report.accepted)), file=out)
        return 0
    finally:
        ctl.close()


def cmd_generate(
    cfg: Config,
    mode: str,
    out: IO[str],
    *,
    execution: ContractExecution | None = None,
) -> int:
    # Lazy, here and in the two commands below: pipeline pulls in deepdiff and
    # the whole HCL stack, ~40ms that --help, --version and a config error
    # should not pay for.
    from .pipeline import run_generate  # noqa: PLC0415

    return run_generate(
        cfg,
        mode,
        out,
        runner=execution.runner if execution is not None else None,
        provider_schema=execution.schema if execution is not None else None,
    )


def cmd_reconcile(
    cfg: Config,
    out: IO[str],
    check: bool = False,
    *,
    execution: ContractExecution | None = None,
) -> int:
    from .pipeline import run_reconcile  # noqa: PLC0415

    return run_reconcile(
        cfg,
        out,
        check=check,
        runner=execution.runner if execution is not None else None,
        provider_schema=execution.schema if execution is not None else None,
    )


def cmd_verify(
    cfg: Config, out: IO[str], *, execution: ContractExecution | None = None
) -> int:
    from .pipeline import run_verify  # noqa: PLC0415

    return run_verify(
        cfg,
        out,
        runner=execution.runner if execution is not None else None,
        provider_schema=execution.schema if execution is not None else None,
    )


def cmd_migrate(
    cfg: Config,
    out: IO[str],
    *,
    write_baseline: bool,
    execution: ContractExecution | None = None,
) -> int:
    from .pipeline import run_migrate  # noqa: PLC0415

    return run_migrate(
        cfg,
        out,
        write_baseline=write_baseline,
        runner=execution.runner if execution is not None else None,
        provider_schema=execution.schema if execution is not None else None,
    )


def _cannot_reach(cfg: Config, exc: Exception) -> int:
    print(
        f"ubitofu: cannot reach the UniFi controller ({cfg.controller_url}): {exc}",
        file=sys.stderr,
    )
    return EXIT_CONTROLLER_UNREACHABLE


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
        execution = resolve_configured_execution(cfg)
    except (ConfigError, ContractError) as exc:
        print(f"ubitofu: config error: {exc}", file=sys.stderr)
        return 2
    try:
        if args.command == "enumerate":
            return cmd_enumerate(cfg, args.mode, sys.stdout, execution=execution)
        if args.command == "generate":
            return cmd_generate(cfg, args.mode, sys.stdout, execution=execution)
        if args.command == "reconcile":
            return cmd_reconcile(
                cfg,
                sys.stdout,
                check=getattr(args, "check", False),
                execution=execution,
            )
        if args.command == "migrate":
            return cmd_migrate(
                cfg, sys.stdout,
                write_baseline=getattr(args, "write_baseline", False),
                execution=execution,
            )
        return cmd_verify(cfg, sys.stdout, execution=execution)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code in (401, 403):
            print(
                f"ubitofu: authentication failed for {cfg.controller_url}"
                " — check username/password or API key",
                file=sys.stderr,
            )
            return EXIT_AUTH_FAILED
        # Any other status means the controller answered and the answer was an
        # error. That is not literally "unreachable", but the operator does the
        # same thing either way — look at the controller — so it shares 20
        # rather than earning a code nobody would branch on differently.
        return _cannot_reach(cfg, exc)
    except httpx.HTTPError as exc:
        return _cannot_reach(cfg, exc)
    except TofuError as exc:
        print(f"ubitofu: tofu failed: {exc}", file=sys.stderr)
        return EXIT_TOFU_FAILED
    except subprocess.CalledProcessError:
        # `op read` is the only check=True subprocess in the codebase, so this
        # is unambiguously secret retrieval. tofu goes through TofuRunner,
        # which inspects the return code itself and raises TofuError.
        print(
            "ubitofu: 1Password not signed in or key missing"
            " — run 'op signin' / set the api-key source",
            file=sys.stderr,
        )
        return EXIT_SECRET_UNAVAILABLE
    except Exception as exc:  # noqa: BLE001
        print(
            f"ubitofu: unexpected error: {type(exc).__name__}: {exc} (please report)",
            file=sys.stderr,
        )
        return 1
