# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 James Braid
"""Public, bounded interpretation of provider and controller coverage."""

from __future__ import annotations

import hashlib
import json

from .config import Config
from .controller import Controller
from .coverage import (
    Finding,
    audit_coverage_snapshot,
    collect_coverage_snapshot,
    digest_coverage_report,
)
from .errors import ControllerResponseError, ExternalDocumentError
from .outcomes import CommandOutcome, OutcomeItem, opaque_reference
from .tofu_json import validate_document_header
from .tofu_runner import TofuRunner


def inspect_coverage(
    *,
    cfg: Config,
    controller: Controller,
    runner: TofuRunner,
) -> CommandOutcome:
    """Inspect coverage while exposing only closed messages and opaque identities."""
    del cfg
    schema = runner.providers_schema()
    validate_document_header(schema, kind="provider_schema")
    try:
        snapshot = collect_coverage_snapshot(controller)
    except ControllerResponseError as exc:
        raise ControllerResponseError(
            opaque_reference(f"coverage-endpoint:{exc.endpoint_id}"),
            exc.status,
            exc.reason,
        ) from exc
    try:
        report = audit_coverage_snapshot(snapshot, schema)
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        raise ExternalDocumentError(
            "provider_schema", "coverage", "invalid document"
        ) from exc
    absent = tuple(
        OutcomeItem(
            "unsupported_endpoint",
            "warning",
            opaque_reference(f"coverage-endpoint:{observation.endpoint_id}"),
            "controller endpoint is unsupported",
        )
        for observation in snapshot.observations
        if observation.policy_absent
    )
    items = tuple(_finding_item(finding) for finding in report.gaps) + absent + (
        OutcomeItem("inspection_complete", "info", None, "inspection is complete"),
    )
    return CommandOutcome(
        command="inspect",
        changed=False,
        blocked=False,
        summary="inspection complete",
        items=items,
        input_digests=(
            ("controller", digest_coverage_report(report)),
            ("provider_schema", _json_digest(schema)),
        ),
        payload=None,
    )


def _finding_item(finding: Finding) -> OutcomeItem:
    reason = (
        "unmapped_controller_resource"
        if finding.kind in {"endpoint", "object"}
        else "coverage_gap"
    )
    message = (
        "controller resource is unmapped"
        if reason == "unmapped_controller_resource"
        else "controller coverage is incomplete"
    )
    return OutcomeItem(
        reason,
        "warning",
        opaque_reference(f"{finding.kind}:{finding.identifier}"),
        message,
    )


def _json_digest(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()
