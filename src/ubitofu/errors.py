# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 James Braid
"""Typed operational failures whose public rendering never includes raw input."""

import re
from dataclasses import dataclass

_SAFE_TOKEN = re.compile(r"^[a-zA-Z0-9_./{}:-]{1,120}$")
_SAFE_REASONS = frozenset(
    {
        "authentication failed",
        "controller request failed",
        "endpoint unavailable",
        "execution failed",
        "forbidden command",
        "invalid collection envelope",
        "invalid document",
        "malformed JSON",
        "missing field",
        "plan reported errors",
        "rate limited",
        "run tofu init before retrying",
        "server error",
        "unsupported format version",
    }
)


class UbitofuError(RuntimeError):
    """Expected operational failure with safe operator context."""

    def __str__(self) -> str:
        return render_safe_error(self)


@dataclass(init=False)
class TofuExecutionError(UbitofuError):
    command: str
    exit_code: int
    reason: str

    def __init__(self, command: str, exit_code: int, reason: str) -> None:
        RuntimeError.__init__(self)
        object.__setattr__(self, "command", command)
        object.__setattr__(self, "exit_code", exit_code)
        object.__setattr__(self, "reason", reason)


@dataclass(init=False)
class ExternalDocumentError(UbitofuError):
    kind: str
    field: str
    reason: str

    def __init__(self, kind: str, field: str, reason: str) -> None:
        RuntimeError.__init__(self)
        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "field", field)
        object.__setattr__(self, "reason", reason)


@dataclass(init=False)
class ControllerResponseError(UbitofuError):
    endpoint_id: str
    status: int | None
    reason: str

    def __init__(self, endpoint_id: str, status: int | None, reason: str) -> None:
        RuntimeError.__init__(self)
        object.__setattr__(self, "endpoint_id", endpoint_id)
        object.__setattr__(self, "status", status)
        object.__setattr__(self, "reason", reason)


@dataclass(init=False)
class ProviderContractError(UbitofuError):
    """The selected provider evidence cannot be admitted safely."""

    reason: str

    def __init__(self, reason: str) -> None:
        RuntimeError.__init__(self)
        object.__setattr__(self, "reason", reason)


def _token(value: str, fallback: str) -> str:
    return value if _SAFE_TOKEN.fullmatch(value) else fallback


def _reason(value: str) -> str:
    return value if value in _SAFE_REASONS else "operation failed"


def render_safe_error(error: BaseException) -> str:
    """Return one bounded diagnostic built only from allowlisted typed fields."""
    if isinstance(error, TofuExecutionError):
        return (
            f"tofu {_token(error.command, 'command')} failed "
            f"(exit {error.exit_code}): {_reason(error.reason)}"
        )
    if isinstance(error, ExternalDocumentError):
        return (
            f"invalid {_token(error.kind, 'external')} document "
            f"({_token(error.field, 'field')}): {_reason(error.reason)}"
        )
    if isinstance(error, ControllerResponseError):
        status = "no status" if error.status is None else f"HTTP {error.status}"
        return (
            f"controller {_token(error.endpoint_id, 'endpoint')} failed "
            f"({status}): {_reason(error.reason)}"
        )
    if isinstance(error, ProviderContractError):
        return "provider contract is invalid"
    return "unexpected internal error; rerun with local debug logging and report the command"
