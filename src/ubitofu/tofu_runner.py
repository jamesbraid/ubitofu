# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 James Braid
import json
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, cast

from .errors import ExternalDocumentError, TofuExecutionError
from .tofu_json import validate_document_header

# Guard by MUTATION, not by "not-plan". Read-only inspection (show, state
# list/show, providers schema, output) is allowed — incremental mode needs it.
FORBIDDEN_COMMANDS = frozenset(
    {"apply", "destroy", "import", "taint", "untaint", "force-unlock", "refresh"}
)  # refresh writes state
FORBIDDEN_STATE_SUBCOMMANDS = frozenset({"rm", "mv", "replace-provider", "push"})
FORBIDDEN_WORKSPACE_SUBCOMMANDS = frozenset({"delete", "new"})


@dataclass
class TofuRunner:
    workdir: Path
    binary: str = "tofu"
    _runner: Callable[..., subprocess.CompletedProcess[str]] = field(default=subprocess.run)

    def _guard(self, args: list[str]) -> None:
        if not args:
            return
        cmd = args[0]
        if cmd in FORBIDDEN_COMMANDS:
            raise TofuExecutionError(cmd, 2, "forbidden command")
        if cmd == "state" and len(args) > 1 and args[1] in FORBIDDEN_STATE_SUBCOMMANDS:
            raise TofuExecutionError("state", 2, "forbidden command")
        if cmd == "workspace" and len(args) > 1 and args[1] in FORBIDDEN_WORKSPACE_SUBCOMMANDS:
            raise TofuExecutionError("workspace", 2, "forbidden command")

    def _exec(self, args: list[str]) -> subprocess.CompletedProcess[str]:
        self._guard(args)
        return self._runner(
            [self.binary, *args],
            cwd=str(self.workdir),
            capture_output=True,
            text=True,
        )

    def _run(
        self, args: list[str], *, allowed_exit_codes: frozenset[int] = frozenset({0})
    ) -> subprocess.CompletedProcess[str]:
        proc = self._exec(args)
        if proc.returncode not in allowed_exit_codes:
            raise TofuExecutionError(args[0], proc.returncode, "execution failed")
        return proc

    def plan(
        self,
        *,
        out: Path | None = None,
        generate_config_out: Path | None = None,
    ) -> int:
        args = ["plan", "-input=false", "-detailed-exitcode"]
        if out is not None:
            args.append(f"-out={out}")
        if generate_config_out is not None:
            args.append(f"-generate-config-out={generate_config_out}")
            try:
                return self._run(args, allowed_exit_codes=frozenset({0, 2})).returncode
            except TofuExecutionError:
                generate_config_out.unlink(missing_ok=True)
                raise
        return self._run(args, allowed_exit_codes=frozenset({0, 2})).returncode

    def _json_document(
        self,
        args: list[str],
        *,
        kind: Literal["plan", "state", "provider_schema"],
    ) -> dict[str, Any]:
        proc = self._run(args)
        try:
            value: object = json.loads(proc.stdout)
        except json.JSONDecodeError as exc:
            raise ExternalDocumentError(kind, "json", "malformed JSON") from exc
        _, document = validate_document_header(value, kind=kind)
        return cast(dict[str, Any], document)

    def show_json(self, plan_file: Path) -> dict[str, Any]:
        return self._json_document(["show", "-json", str(plan_file)], kind="plan")

    def show_state_json(self) -> dict[str, Any]:
        return self._json_document(["show", "-json"], kind="state")

    def providers_schema(self) -> dict[str, Any]:
        return self._json_document(["providers", "schema", "-json"], kind="provider_schema")

    def is_clean(self, exit_code: int) -> bool:
        return exit_code == 0
