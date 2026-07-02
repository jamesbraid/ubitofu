# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 James Braid
import argparse
import os
import sys
from typing import IO

from .config import Config, load_config, resolve_api_key
from .controller import Controller
from .enumerator import enumerate_controller
from .import_emitter import emit_import_blocks
from .reporter import format_gaps


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="unifi-tofu-import",
        description="Plan-only UniFi -> OpenTofu importer.",
    )
    sub = p.add_subparsers(dest="command", required=True)
    for name in ("enumerate", "generate", "verify"):
        sp = sub.add_parser(name)
        sp.add_argument("--config", required=True)
        sp.add_argument("--controller-url")
        sp.add_argument("--site")
        sp.add_argument("--api-key-source", choices=["op", "env"])
        if name != "verify":
            sp.add_argument("--mode", choices=["bulk", "incremental"], default="bulk")
    return p


def _controller(cfg: Config) -> Controller:
    key = resolve_api_key(cfg, environ=os.environ)
    return Controller(base_url=cfg.controller_url, site=cfg.site, api_key=key)


def cmd_enumerate(cfg: Config, mode: str, out: IO[str]) -> int:
    res = enumerate_controller(_controller(cfg))
    print(emit_import_blocks(res.targets), file=out)
    print(format_gaps(res.gaps), file=out)
    return 0


def cmd_generate(cfg: Config, mode: str, out: IO[str]) -> int:
    # pipeline.run_generate wires Tasks 1-9 end-to-end (implemented in Task 12).
    # Lazy import so cli is importable before Task 12 exists.
    from .pipeline import run_generate  # noqa: PLC0415

    return run_generate(cfg, mode, out)


def cmd_verify(cfg: Config, out: IO[str]) -> int:
    from .pipeline import run_verify  # noqa: PLC0415

    return run_verify(cfg, out)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    cfg = load_config(args.config)
    # CLI flags override config-file values.
    if args.controller_url:
        cfg.controller_url = args.controller_url
    if args.site:
        cfg.site = args.site
    if args.api_key_source:
        cfg.api_key_source = args.api_key_source
    if args.command == "enumerate":
        return cmd_enumerate(cfg, args.mode, sys.stdout)
    if args.command == "generate":
        return cmd_generate(cfg, args.mode, sys.stdout)
    return cmd_verify(cfg, sys.stdout)
