# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 James Braid
"""Strict headers for JSON emitted by the OpenTofu subprocess boundary."""

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

from .errors import ExternalDocumentError

_FORMAT_VERSION = re.compile(r"^(\d+)\.(\d+)$")


@dataclass(frozen=True)
class DocumentHeader:
    kind: Literal["plan", "state", "provider_schema"]
    format_version: tuple[int, int]


def validate_document_header(
    value: object,
    *,
    kind: Literal["plan", "state", "provider_schema"],
) -> tuple[DocumentHeader, Mapping[str, object]]:
    if not isinstance(value, Mapping):
        raise ExternalDocumentError(kind, "document", "invalid document")
    version = value.get("format_version")
    if not isinstance(version, str):
        raise ExternalDocumentError(kind, "format_version", "missing field")
    match = _FORMAT_VERSION.fullmatch(version)
    if match is None:
        raise ExternalDocumentError(kind, "format_version", "invalid document")
    parsed = (int(match.group(1)), int(match.group(2)))
    if parsed[0] != 1:
        raise ExternalDocumentError(kind, "format_version", "unsupported format version")
    if kind == "plan":
        errored = value.get("errored")
        if not isinstance(errored, bool):
            raise ExternalDocumentError(kind, "errored", "missing field")
        if errored:
            raise ExternalDocumentError(kind, "errored", "plan reported errors")
    return DocumentHeader(kind, parsed), value
