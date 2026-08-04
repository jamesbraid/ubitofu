# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 James Braid
"""Tests for typed controller-health policy."""

from __future__ import annotations

import json

import pytest

from ubitofu.controller import Controller
from ubitofu.errors import ControllerResponseError, UbitofuError
from ubitofu.outcomes import exit_code, opaque_reference, render_json


class HealthController(Controller):
    def __init__(self, records: list[dict[str, object]]) -> None:
        self.site = "default"
        self.records = records

    def collection(self, endpoint: str) -> list[dict[str, object]]:
        assert endpoint == "stat/health"
        return self.records


def test_capture_health_normalizes_the_closed_status_vocabulary() -> None:
    """Catches raw subsystem/status strings or incorrect ranks escaping policy."""
    from ubitofu.health import capture_health

    snapshot = capture_health(
        HealthController(
            [
                {"subsystem": "wlan", "status": "warning"},
                {"subsystem": "wan", "status": "ok"},
                {"subsystem": "lan", "status": "error"},
                {"subsystem": "vpn", "status": "unknown"},
            ]
        )
    )

    assert [(item.subsystem, item.status, item.rank) for item in snapshot.subsystems] == sorted(
        [
            (opaque_reference("health-subsystem:lan"), "error", 2),
            (opaque_reference("health-subsystem:vpn"), "unknown", 3),
            (opaque_reference("health-subsystem:wan"), "ok", 0),
            (opaque_reference("health-subsystem:wlan"), "warning", 1),
        ]
    )


@pytest.mark.parametrize(
    "records",
    [
        [{"subsystem": "wan"}],
        [{"status": "ok"}],
        [{"subsystem": "wan", "status": "healthy"}],
        [{"subsystem": "wan", "status": "ok"}, {"subsystem": "wan", "status": "error"}],
    ],
)
def test_capture_health_rejects_missing_unknown_and_duplicate_fields(records) -> None:
    """Catches malformed health data being silently treated as healthy."""
    from ubitofu.health import capture_health

    with pytest.raises(ControllerResponseError, match="invalid document"):
        capture_health(HealthController(records))


def test_capture_health_rejects_an_empty_baseline() -> None:
    """Catches endpoint absence or an empty response being authorized as healthy."""
    from ubitofu.health import capture_health

    with pytest.raises(ControllerResponseError, match="invalid document"):
        capture_health(HealthController([]))


def test_typed_health_rejects_boolean_or_mismatched_ranks() -> None:
    """Catches Python boolean integers bypassing the strict status/rank vocabulary."""
    from ubitofu.health import SubsystemHealth

    reference = opaque_reference("health-subsystem:wan")
    with pytest.raises(ValueError):
        SubsystemHealth(reference, "warning", True)
    with pytest.raises(ValueError):
        SubsystemHealth(reference, "warning", 2)


def _snapshot(**statuses: str):
    from ubitofu.health import HealthSnapshot, SubsystemHealth

    ranks = {"ok": 0, "warning": 1, "error": 2, "unknown": 3}
    return HealthSnapshot(
        tuple(
            SubsystemHealth(opaque_reference(f"health-subsystem:{name}"), status, ranks[status])
            for name, status in statuses.items()
        )
    )


@pytest.mark.parametrize(
    "before,after,reason,severity,blocked",
    [
        ({"wan": "error"}, {"wan": "warning"}, "health_recovered", "info", False),
        ({"wan": "error"}, {"wan": "error"}, "health_unchanged", "info", False),
        ({"wan": "ok"}, {"wan": "warning"}, "health_degraded", "blocking", True),
        ({"wan": "warning"}, {"wan": "error"}, "health_degraded", "blocking", True),
        ({}, {"wan": "warning"}, "health_degraded", "blocking", True),
        ({}, {"wan": "unknown"}, "health_unknown_new", "blocking", True),
        ({"wan": "ok"}, {"wan": "unknown"}, "health_unknown_new", "blocking", True),
        ({"wan": "unknown"}, {"wan": "unknown"}, "advisory", "warning", False),
        ({"wan": "ok"}, {}, "health_baseline_missing", "blocking", True),
    ],
)
def test_compare_health_applies_baseline_delta_policy(
    before, after, reason, severity, blocked
) -> None:
    """Catches absolute health ranks replacing the required baseline delta policy."""
    from ubitofu.health import compare_health

    outcome = compare_health(_snapshot(**before), _snapshot(**after))

    assert outcome.blocked is blocked
    assert exit_code(outcome) == (3 if blocked else 0)
    assert [(item.reason_code, item.severity) for item in outcome.items] == [
        (reason, severity)
    ]
    assert outcome.items[0].address == opaque_reference("health-subsystem:wan")


def test_compare_health_reports_recovery_and_new_ok_without_blocking() -> None:
    """Catches healthy additions or recoveries being classified as degradation."""
    from ubitofu.health import compare_health

    outcome = compare_health(
        _snapshot(wan="error"),
        _snapshot(wan="ok", wlan="ok"),
    )

    assert outcome.blocked is False
    assert [(item.reason_code, item.address) for item in outcome.items] == [
        ("health_recovered", opaque_reference("health-subsystem:wan")),
        ("health_unchanged", opaque_reference("health-subsystem:wlan")),
    ]


def test_health_snapshot_receipt_round_trips_and_rejects_other_schemas() -> None:
    """Catches compare accepting a receipt from another command or schema version."""
    from ubitofu.health import health_snapshot_from_receipt, health_snapshot_outcome

    snapshot = _snapshot(wan="ok", wlan="unknown")
    raw = render_json(health_snapshot_outcome(snapshot))
    assert health_snapshot_from_receipt(raw) == snapshot

    document = json.loads(raw)
    document["version"] = 2
    with pytest.raises(UbitofuError):
        health_snapshot_from_receipt(json.dumps(document).encode())

    document = json.loads(raw)
    document["outcome"]["command"] = "health_compare"
    document["outcome"]["summary"] = "health comparison complete"
    document["outcome"]["input_digests"] = [
        ["health_before", "a" * 64],
        ["health_after", "b" * 64],
    ]
    with pytest.raises(UbitofuError):
        health_snapshot_from_receipt(json.dumps(document).encode())


def test_health_snapshot_receipt_digest_binds_the_payload() -> None:
    """Catches a baseline payload being changed without its identity changing."""
    from ubitofu.health import health_snapshot_from_receipt, health_snapshot_outcome

    raw = render_json(health_snapshot_outcome(_snapshot(wan="ok")))
    document = json.loads(raw)
    document["outcome"]["payload"]["subsystems"][0]["status"] = "error"
    document["outcome"]["payload"]["subsystems"][0]["rank"] = 2

    with pytest.raises(UbitofuError):
        health_snapshot_from_receipt(json.dumps(document).encode())


def test_health_snapshot_receipt_rejects_a_failed_capture_outcome() -> None:
    """Catches a blocked capture being reused as a valid apply baseline."""
    from ubitofu.health import health_snapshot_from_receipt, health_snapshot_outcome
    from ubitofu.outcomes import CommandOutcome, OutcomeItem

    successful = health_snapshot_outcome(_snapshot(wan="ok"))
    failed = CommandOutcome(
        command="health_snapshot",
        changed=False,
        blocked=True,
        summary="health snapshot complete",
        items=(
            OutcomeItem(
                "health_snapshot_failed",
                "blocking",
                None,
                "health snapshot is unavailable",
            ),
        ),
        input_digests=successful.input_digests,
        payload=successful.payload,
    )

    with pytest.raises(UbitofuError):
        health_snapshot_from_receipt(render_json(failed))


def test_successful_health_receipts_require_at_least_one_subsystem() -> None:
    """Catches callers bypassing capture_health to authorize an empty baseline."""
    from ubitofu.health import HealthSnapshot, health_snapshot_from_receipt, health_snapshot_outcome
    from ubitofu.outcomes import CommandOutcome, OutcomeItem
    from ubitofu.values import freeze_value

    empty = HealthSnapshot(())
    with pytest.raises(ValueError):
        health_snapshot_outcome(empty)

    forged = CommandOutcome(
        command="health_snapshot",
        changed=False,
        blocked=False,
        summary="health snapshot complete",
        items=(
            OutcomeItem(
                "health_snapshot_captured", "info", None, "health snapshot captured"
            ),
        ),
        input_digests=(("controller", empty.sha256),),
        payload=freeze_value({"subsystems": []}),
    )
    with pytest.raises(UbitofuError):
        health_snapshot_from_receipt(render_json(forged))


def test_health_fixtures_capture_and_compare_as_a_degradation(fixtures_dir) -> None:
    """Catches fixture schema drift or compare bypassing the capture normalization path."""
    from ubitofu.health import capture_health, compare_health

    before_records = json.loads((fixtures_dir / "health" / "before.json").read_text())
    after_records = json.loads((fixtures_dir / "health" / "after.json").read_text())
    outcome = compare_health(
        capture_health(HealthController(before_records)),
        capture_health(HealthController(after_records)),
    )

    assert outcome.blocked is True
    assert [(item.reason_code, item.severity) for item in outcome.items] == [
        ("health_degraded", "blocking"),
        ("health_unchanged", "info"),
    ]
