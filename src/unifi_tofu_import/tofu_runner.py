import json
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

# Guard by MUTATION, not by "not-plan". Read-only inspection (show, state
# list/show, providers schema, output) is allowed — incremental mode needs it.
FORBIDDEN_COMMANDS = frozenset(
    {"apply", "destroy", "import", "taint", "untaint", "force-unlock", "refresh"}
)  # refresh writes state
FORBIDDEN_STATE_SUBCOMMANDS = frozenset({"rm", "mv", "replace-provider", "push"})
FORBIDDEN_WORKSPACE_SUBCOMMANDS = frozenset({"delete", "new"})


class TofuError(RuntimeError):
    pass


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
            raise TofuError(f"refusing to run forbidden tofu command: {cmd}")
        if cmd == "state" and len(args) > 1 and args[1] in FORBIDDEN_STATE_SUBCOMMANDS:
            raise TofuError(f"refusing to run forbidden tofu subcommand: state {args[1]}")
        if cmd == "workspace" and len(args) > 1 and args[1] in FORBIDDEN_WORKSPACE_SUBCOMMANDS:
            raise TofuError(f"refusing to run forbidden tofu subcommand: workspace {args[1]}")

    def _run(self, args: list[str]) -> subprocess.CompletedProcess[str]:
        self._guard(args)
        proc = self._runner(
            [self.binary, *args],
            cwd=str(self.workdir),
            capture_output=True,
            text=True,
        )
        # -detailed-exitcode legitimately returns 2 (changes present).
        if proc.returncode not in (0, 2):
            raise TofuError(
                proc.stderr.strip() or f"tofu {args[0]} exited {proc.returncode}"
            )
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
        return self._run(args).returncode

    def show_json(self, plan_file: Path) -> dict[str, Any]:
        return cast(dict[str, Any], json.loads(self._run(["show", "-json", str(plan_file)]).stdout))

    def show_state_json(self) -> dict[str, Any]:
        # `tofu show -json` with no plan file emits current state (read-only).
        # Empty/no state -> "{}"; callers default the values tree safely.
        return cast(dict[str, Any], json.loads(self._run(["show", "-json"]).stdout or "{}"))

    def providers_schema(self) -> dict[str, Any]:
        return cast(dict[str, Any], json.loads(self._run(["providers", "schema", "-json"]).stdout))

    def is_clean(self, exit_code: int) -> bool:
        return exit_code == 0
