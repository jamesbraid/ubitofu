# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 James Braid
"""The complete public command surface for ubitofu 0.10."""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

import httpx

from . import pipeline
from .config import (
    Config,
    ConfigError,
    CredentialConfigError,
    TLSConfigError,
    load_config,
)
from .errors import (
    ControllerResponseError,
    ProviderContractError,
    UbitofuError,
    render_safe_error,
)
from .outcomes import CommandOutcome, emit_output, exit_code

_EXIT_EPILOG = (
    "exit codes (same scheme for every command):\n"
    "  0  success, allowed plan, or advisory warning\n"
    "  1  operational failure\n"
    "  2  command-line usage or configuration error\n"
    "  3  blocking conflict, unsafe plan, or health degradation\n"
)


def _common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--config", required=True)
    parser.add_argument("--format", choices=("human", "json"), default="human")
    parser.add_argument("--output", default="-")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ubitofu",
        description="Generate and reconcile OpenTofu HCL from a live UniFi controller.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("generate", "reconcile", "check", "inspect"):
        command = commands.add_parser(
            name,
            epilog=_EXIT_EPILOG,
            formatter_class=argparse.RawDescriptionHelpFormatter,
        )
        _common(command)
        if name == "reconcile":
            command.add_argument("--dry-run", action="store_true")
        if name == "check":
            command.add_argument("--plan", required=True, type=Path)

    health = commands.add_parser("health")
    health_commands = health.add_subparsers(dest="health_command", required=True)
    snapshot = health_commands.add_parser(
        "snapshot",
        epilog=_EXIT_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    _common(snapshot)
    compare = health_commands.add_parser(
        "compare",
        epilog=_EXIT_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    _common(compare)
    compare.add_argument("--before", required=True, type=Path)
    return parser


def _load_effective_config(args: argparse.Namespace) -> Config:
    cfg = load_config(args.config)
    _validate_output_destination(args, cfg)
    return cfg


def _validate_output_destination(args: argparse.Namespace, cfg: Config) -> None:
    """Reject receipt paths that can overwrite command inputs or owned state."""
    if args.output == "-":
        return
    output = Path(args.output).resolve(strict=False)
    workdir = Path(cfg.workdir).resolve(strict=True)
    protected_inputs = [Path(args.config).resolve(strict=False)]
    plan = getattr(args, "plan", None)
    if plan is not None:
        plan_path = Path(plan)
        protected_inputs.append(
            (plan_path if plan_path.is_absolute() else workdir / plan_path).resolve(
                strict=False
            )
        )
    before = getattr(args, "before", None)
    if before is not None:
        protected_inputs.append(Path(before).resolve(strict=False))
    if output in protected_inputs:
        raise ConfigError("output path collides with a command input")

    try:
        relative = output.relative_to(workdir)
    except ValueError:
        return
    name = relative.name
    is_hcl = (
        name.endswith(".tf")
        or name.endswith(".tofu")
        or name.endswith(".tf.json")
        or name.endswith(".tofu.json")
    )
    if (
        is_hcl
        or name == "COVERAGE.md"
        or name == "ubitofu-imports.tf"
        or (relative.parts and relative.parts[0] == ".ubitofu")
    ):
        raise ConfigError("output path collides with managed worktree content")


def _dispatch(args: argparse.Namespace, cfg: Config) -> CommandOutcome:
    if args.command == "generate":
        return pipeline.run_generate(cfg=cfg)
    if args.command == "reconcile":
        return pipeline.run_reconcile(cfg=cfg, dry_run=args.dry_run)
    if args.command == "check":
        return pipeline.run_check(cfg=cfg, plan_path=args.plan)
    if args.command == "inspect":
        return pipeline.run_inspect(cfg=cfg)
    if args.health_command == "snapshot":
        return pipeline.run_health_snapshot(cfg=cfg)
    return pipeline.run_health_compare(cfg=cfg, before_path=args.before)


def _cannot_reach() -> int:
    print(
        "ubitofu: cannot reach controller -- check connection and TLS settings",
        file=sys.stderr,
    )
    return 1


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        cfg = _load_effective_config(args)
    except (ConfigError, OSError, ValueError):
        print("ubitofu: config error: invalid configuration", file=sys.stderr)
        return 2
    try:
        outcome = _dispatch(args, cfg)
        emit_output(
            outcome,
            format=args.format,
            output=args.output,
            stdout=sys.stdout,
            owner_root=Path(cfg.workdir),
        )
        return exit_code(outcome)
    except CredentialConfigError as exc:
        label = "API key" if exc.credential == "api_key" else "password"
        print(
            f"ubitofu: config error: configured {label} environment variable is unset",
            file=sys.stderr,
        )
        return 2
    except TLSConfigError:
        print("ubitofu: config error: custom CA bundle is invalid", file=sys.stderr)
        return 2
    except httpx.HTTPStatusError as error:
        if error.response.status_code in (401, 403):
            print("ubitofu: authentication failed -- check credentials", file=sys.stderr)
            return 1
        return _cannot_reach()
    except httpx.HTTPError:
        return _cannot_reach()
    except ControllerResponseError as exc:
        if exc.status is None:
            return _cannot_reach()
        if exc.status in (401, 403) or exc.reason == "authentication failed":
            print("ubitofu: authentication failed -- check credentials", file=sys.stderr)
            return 1
        print(f"ubitofu: {render_safe_error(exc)}", file=sys.stderr)
        return 1
    except ProviderContractError as exc:
        print(f"ubitofu: config error: {render_safe_error(exc)}", file=sys.stderr)
        return 2
    except UbitofuError as exc:
        print(f"ubitofu: {render_safe_error(exc)}", file=sys.stderr)
        return 1
    except subprocess.CalledProcessError:
        print(
            "ubitofu: 1Password is unavailable -- sign in or select an environment source",
            file=sys.stderr,
        )
        return 1
    except Exception:  # noqa: BLE001
        print(
            "ubitofu: unexpected internal error; rerun with local debug logging",
            file=sys.stderr,
        )
        return 1
