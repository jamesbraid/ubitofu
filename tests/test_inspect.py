# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 James Braid
"""Tests for the public inspection policy."""

import json

import pytest

from ubitofu.config import Config
from ubitofu.controller import Controller
from ubitofu.errors import ControllerResponseError
from ubitofu.outcomes import opaque_reference, render_json
from ubitofu.values import freeze_value


class EmptyController(Controller):
    def __init__(self) -> None:
        self.site = "default"
        self.calls: list[str] = []

    def collection_observation(self, endpoint: str):
        from ubitofu.controller import CollectionObservation

        self.calls.append(endpoint)
        return CollectionObservation(endpoint, (), False)


class InspectionController(EmptyController):
    def __init__(
        self,
        *,
        records: dict[str, list[dict[str, object]]] | None = None,
        absent: set[str] | None = None,
        error: tuple[str, int | None, str] | None = None,
    ) -> None:
        super().__init__()
        self.records = records or {}
        self.absent = absent or set()
        self.error = error

    def collection_observation(self, endpoint: str):
        from ubitofu.controller import CollectionObservation

        self.calls.append(endpoint)
        if self.error is not None and endpoint == self.error[0]:
            raise ControllerResponseError(*self.error)
        frozen = tuple(freeze_value(record) for record in self.records.get(endpoint, []))
        return CollectionObservation(endpoint, frozen, endpoint in self.absent)


class SchemaRunner:
    def __init__(self, schema: dict[str, object]) -> None:
        self.schema = schema

    def providers_schema(self) -> dict[str, object]:
        return self.schema


def test_supported_empty_endpoints_are_a_clean_inspection(fixtures_dir) -> None:
    """Catches supported empty collections being mislabeled as absent or broken."""
    from ubitofu.inspect import inspect_coverage

    schema = json.loads((fixtures_dir / "coverage" / "providers_schema.json").read_text())
    outcome = inspect_coverage(
        cfg=Config(controller_url="https://unifi.example", site="default"),
        controller=EmptyController(),
        runner=SchemaRunner(schema),
    )

    assert outcome.command == "inspect"
    assert outcome.blocked is False
    assert [(item.reason_code, item.severity) for item in outcome.items] == [
        ("coverage_gap", "warning"),
        ("inspection_complete", "info"),
    ]


def _inspect(fixtures_dir, controller: Controller):
    from ubitofu.inspect import inspect_coverage

    schema = json.loads((fixtures_dir / "coverage" / "providers_schema.json").read_text())
    return inspect_coverage(
        cfg=Config(controller_url="https://unifi.example", site="default"),
        controller=controller,
        runner=SchemaRunner(schema),
    )


def test_policy_accepted_endpoint_absence_is_advisory_and_opaque(fixtures_dir) -> None:
    """Catches explicit absence policy being hidden or exposed as a raw endpoint."""
    endpoint = "rest/hotspot2conf"
    outcome = _inspect(fixtures_dir, InspectionController(absent={endpoint}))

    item = next(item for item in outcome.items if item.reason_code == "unsupported_endpoint")
    assert (item.severity, item.address, item.message) == (
        "warning",
        opaque_reference(f"coverage-endpoint:{endpoint}"),
        "controller endpoint is unsupported",
    )
    assert endpoint.encode() not in render_json(outcome)


def test_schema_gap_and_unmapped_controller_object_stay_distinct(fixtures_dir) -> None:
    """Catches provider schema gaps and live unmapped objects sharing one diagnosis."""
    controller = InspectionController(
        records={
            "get/setting": [{"key": "mgmt", "new_field": True}],
            "v2/api/site/{site}/nat": [{"_id": "secret-object", "type": "MASQUERADE"}],
        }
    )
    outcome = _inspect(fixtures_dir, controller)

    reasons = {item.reason_code for item in outcome.items}
    assert "coverage_gap" in reasons
    assert "unmapped_controller_resource" in reasons
    rendered = render_json(outcome)
    assert b"new_field" not in rendered
    assert b"secret-object" not in rendered
    assert b"MASQUERADE" not in rendered


@pytest.mark.parametrize(
    "status,reason",
    [
        (401, "authentication failed"),
        (429, "rate limited"),
        (200, "invalid document"),
        (503, "server error"),
    ],
)
def test_operational_inspection_failures_remain_typed_and_use_opaque_endpoint_ids(
    fixtures_dir, status, reason
) -> None:
    """Catches transport failures being downgraded to nonblocking coverage findings."""
    endpoint = "v2/api/site/{site}/nat"
    controller = InspectionController(error=(endpoint, status, reason))

    with pytest.raises(ControllerResponseError) as exc_info:
        _inspect(fixtures_dir, controller)

    assert (exc_info.value.status, exc_info.value.reason) == (status, reason)
    assert exc_info.value.endpoint_id == opaque_reference(f"coverage-endpoint:{endpoint}")
    assert endpoint not in str(exc_info.value)


def test_controller_digest_covers_only_the_nonsecret_policy_projection(fixtures_dir) -> None:
    """Catches a public digest becoming an oracle for policy-equivalent raw values."""
    first = _inspect(
        fixtures_dir,
        InspectionController(records={"get/setting": [{"key": "mgmt", "led_enabled": True}]}),
    )
    second = _inspect(
        fixtures_dir,
        InspectionController(records={"get/setting": [{"key": "mgmt", "led_enabled": False}]}),
    )
    structural_gap = _inspect(
        fixtures_dir,
        InspectionController(
            records={
                "get/setting": [
                    {"key": "mgmt", "led_enabled": True, "new_field": "secret-shaped-value"}
                ]
            }
        ),
    )

    assert first.items == second.items
    assert dict(first.input_digests)["controller"] == dict(second.input_digests)["controller"]
    assert dict(first.input_digests)["controller"] != dict(structural_gap.input_digests)[
        "controller"
    ]
    assert b"secret-shaped-value" not in render_json(structural_gap)


def test_inspection_reads_each_controller_endpoint_once(fixtures_dir) -> None:
    """Catches digest and policy using observations from different collection windows."""
    controller = EmptyController()
    _inspect(fixtures_dir, controller)
    assert len(controller.calls) == len(set(controller.calls))
