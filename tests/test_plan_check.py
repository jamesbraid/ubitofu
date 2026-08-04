# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 James Braid
"""Saved-plan authorization against one exact file and fresh controller view."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest

from ubitofu.config import Config
from ubitofu.enumerator import EnumerationResult, ImportTarget
from ubitofu.errors import ExternalDocumentError, UbitofuError
from ubitofu.outcomes import digest_active_source
from ubitofu.reconcile_model import ControllerRecord
from ubitofu.values import FrozenObject, freeze_value

_DEFAULT_FRESH = object()


def _object(value: object) -> FrozenObject:
    frozen = freeze_value(value)
    assert isinstance(frozen, FrozenObject)
    return frozen


class RecordingRunner:
    def __init__(
        self,
        workdir: Path,
        document: dict[str, object],
        schema: dict[str, object],
        *,
        during_show=None,
    ) -> None:
        self.workdir = workdir
        self.document = document
        self.schema = schema
        self.during_show = during_show
        self.calls: list[tuple[str, Path | None]] = []

    def show_json(self, plan_file: Path) -> dict[str, object]:
        self.calls.append(("show", plan_file))
        if self.during_show is not None:
            self.during_show()
        if self.document.get("errored") is True:
            raise ExternalDocumentError("plan", "errored", "plan reported errors")
        return self.document

    def providers_schema(self) -> dict[str, object]:
        self.calls.append(("providers_schema", None))
        return self.schema

    def plan(self, **_kwargs: object) -> int:
        raise AssertionError("saved-plan check must not create a plan")

    def show_state_json(self) -> dict[str, object]:
        raise AssertionError("saved-plan check must not read current backend state")


def _resource_row(
    *,
    address: str,
    resource_type: str,
    name: str,
    values: dict[str, object],
    module_address: str | None = None,
) -> dict[str, object]:
    row: dict[str, object] = {
        "address": address,
        "mode": "managed",
        "type": resource_type,
        "name": name,
        "values": values,
    }
    if module_address is not None:
        row["module_address"] = module_address
    return row


def _plan_document(
    *,
    base: dict[str, object] | None = None,
    desired: dict[str, object] | None = None,
    plan_live: dict[str, object] | None = None,
    actions: tuple[str, ...] = ("no-op",),
    resource_type: str = "unifi_network",
    name: str = "lan",
    module_address: str | None = None,
    after_unknown: dict[str, object] | None = None,
    errored: bool = False,
) -> dict[str, object]:
    address = f"{resource_type}.{name}"
    if module_address is not None:
        address = f"{module_address}.{address}"
    prior_resources = []
    if base is not None:
        prior_resources.append(
            _resource_row(
                address=address,
                resource_type=resource_type,
                name=name,
                values=base,
                module_address=module_address,
            )
        )
    change = _resource_row(
        address=address,
        resource_type=resource_type,
        name=name,
        values=desired or {},
        module_address=module_address,
    )
    change.pop("values")
    change["change"] = {
        "actions": list(actions),
        "before": plan_live,
        "after": desired,
        "after_unknown": after_unknown or {},
    }
    return {
        "format_version": "1.0",
        "errored": errored,
        "prior_state": {
            "format_version": "1.0",
            "values": {"root_module": {"resources": prior_resources}},
        },
        "resource_changes": [change],
    }


def _schema(
    resource_type: str = "unifi_network",
    *,
    vlan_schema: dict[str, object] | None = None,
) -> dict[str, object]:
    identity = (
        {"mac": {"type": "string", "optional": True}}
        if resource_type == "unifi_device"
        else {"id": {"type": "string", "computed": True}}
    )
    return {
        "format_version": "1.0",
        "provider_schemas": {
            "synthetic/provider": {
                "resource_schemas": {
                    resource_type: {
                        "block": {
                            "attributes": {
                                **identity,
                                "name": {"type": "string", "optional": True},
                                "vlan": vlan_schema
                                or {"type": "number", "optional": True},
                            }
                        }
                    }
                }
            }
        },
    }


def _enumeration(
    fresh: dict[str, object] | None,
    *,
    resource_type: str = "unifi_network",
) -> EnumerationResult:
    result = EnumerationResult(covered_resource_types=[resource_type])
    if fresh is None:
        return result
    import_id = str(fresh.get("mac") or fresh.get("_id"))
    result.targets.append(
        ImportTarget(resource_type, str(fresh.get("name", "resource")), import_id)
    )
    result.records.append(ControllerRecord(resource_type, import_id, _object(fresh)))
    return result


def _write_source(
    workdir: Path,
    *,
    resource_type: str = "unifi_network",
    name: str = "lan",
    include_resource: bool = True,
) -> bytes:
    source = (
        f'resource "{resource_type}" "{name}" {{\n'
        '  name = "synthetic"\n'
        "  vlan = 10\n"
        "}\n"
    ).encode()
    if not include_resource:
        source = b"# intentionally absent from configuration\n"
    (workdir / "main.tf").write_bytes(source)
    return source


def _run_check(
    monkeypatch,
    tmp_path: Path,
    *,
    document: dict[str, object] | None = None,
    fresh: dict[str, object] | None | object = _DEFAULT_FRESH,
    resource_type: str = "unifi_network",
    name: str = "lan",
    include_resource: bool = True,
    during_show=None,
    plan_path: Path | None = None,
    schema: dict[str, object] | None = None,
    enumeration_hook=None,
):
    from ubitofu import plan_check

    source = _write_source(
        tmp_path,
        resource_type=resource_type,
        name=name,
        include_resource=include_resource,
    )
    supplied = tmp_path / "saved.tfplan" if plan_path is None else plan_path
    if plan_path is None:
        supplied.write_bytes(b"exact opaque saved plan bytes")
    plan = document or _plan_document(
        base={"id": "synthetic-id", "name": "synthetic", "vlan": 10},
        desired={"id": "synthetic-id", "name": "synthetic", "vlan": 10},
        plan_live={"id": "synthetic-id", "name": "synthetic", "vlan": 10},
    )
    controller_view = (
        {"_id": "synthetic-id", "name": "synthetic", "vlan": 10}
        if fresh is _DEFAULT_FRESH
        else fresh
    )
    if enumeration_hook is None:
        def enumeration_hook(_controller, *, capture_records):
            return _enumeration(
                controller_view, resource_type=resource_type  # type: ignore[arg-type]
            )

    monkeypatch.setattr(plan_check, "enumerate_controller", enumeration_hook)
    runner = RecordingRunner(
        tmp_path,
        plan,
        schema or _schema(resource_type),
        during_show=during_show,
    )
    if supplied.is_file() and not supplied.is_symlink():
        with supplied.open("rb") as handle:
            before = handle.read()
    else:
        before = None
    outcome = plan_check.check_saved_plan(
        cfg=Config("https://controller.invalid", "default", workdir=str(tmp_path)),
        plan_path=supplied,
        controller=object(),
        runner=runner,
    )
    if supplied.is_file() and not supplied.is_symlink():
        with supplied.open("rb") as handle:
            after = handle.read()
    else:
        after = None
    return outcome, runner, source, before, after


def _reason_codes(outcome) -> set[str]:
    return {item.reason_code for item in outcome.items}


def test_check_reads_only_exact_supplied_plan_and_reports_four_digests(monkeypatch, tmp_path):
    """Catches re-planning, backend-state substitution, or digesting another plan."""
    outcome, runner, source, before, after = _run_check(monkeypatch, tmp_path)

    assert runner.calls == [
        ("show", tmp_path / "saved.tfplan"),
        ("providers_schema", None),
    ]
    assert outcome.blocked is False
    assert _reason_codes(outcome) == {"no_change", "plan_allowed"}
    assert dict(outcome.input_digests)["saved_plan"] == hashlib.sha256(before).hexdigest()
    assert dict(outcome.input_digests)["active_source"] == digest_active_source(
        [(Path("main.tf"), source)]
    )
    assert set(dict(outcome.input_digests)) == {
        "saved_plan",
        "active_source",
        "plan_time_live",
        "fresh_controller",
    }
    assert before == after


def test_check_shows_the_absolute_path_whose_bytes_were_hashed(monkeypatch, tmp_path):
    """Catches an option-shaped relative spelling selecting a different plan."""
    selected = tmp_path / "-plan=other.tfplan"
    selected.write_bytes(b"selected opaque saved plan bytes")
    (tmp_path / "other.tfplan").write_bytes(b"alternate opaque saved plan bytes")

    outcome, runner, *_ = _run_check(
        monkeypatch,
        tmp_path,
        plan_path=Path("-plan=other.tfplan"),
    )

    assert runner.calls[0] == ("show", selected)
    assert dict(outcome.input_digests)["saved_plan"] == hashlib.sha256(
        b"selected opaque saved plan bytes"
    ).hexdigest()


def test_check_hashes_plan_from_open_descriptor_not_path_read_bytes(monkeypatch, tmp_path):
    """Catches a pathname convenience read replacing descriptor-bound hashing."""
    original = Path.read_bytes
    plan_directory = tmp_path / "plans"
    plan_directory.mkdir()
    plan_path = plan_directory / "saved.tfplan"
    plan_path.write_bytes(b"exact opaque saved plan bytes")

    def guarded_read(path: Path) -> bytes:
        if path == plan_path:
            raise AssertionError("plan bytes must be read through an opened descriptor")
        return original(path)

    monkeypatch.setattr(Path, "read_bytes", guarded_read)

    outcome, *_ = _run_check(monkeypatch, tmp_path, plan_path=plan_path)

    assert outcome.blocked is False


@pytest.mark.parametrize("kind", ["missing", "empty", "symlink", "directory", "fifo"])
def test_check_rejects_plan_that_is_not_one_nonempty_regular_file(monkeypatch, tmp_path, kind):
    """Catches absent or special files crossing the OpenTofu subprocess boundary."""
    from ubitofu.plan_check import check_saved_plan

    path = tmp_path / "saved.tfplan"
    if kind == "empty":
        path.write_bytes(b"")
    elif kind == "symlink":
        target = tmp_path / "target.tfplan"
        target.write_bytes(b"saved plan")
        path.symlink_to(target)
    elif kind == "directory":
        path.mkdir()
    elif kind == "fifo":
        os.mkfifo(path)
    runner = RecordingRunner(tmp_path, {}, {})

    with pytest.raises(UbitofuError):
        check_saved_plan(
            cfg=Config("https://controller.invalid", "default", workdir=str(tmp_path)),
            plan_path=path,
            controller=object(),
            runner=runner,
        )
    assert runner.calls == []


@pytest.mark.parametrize("mutation", ["replace", "rewrite"])
def test_check_rejects_plan_replaced_or_mutated_during_show(monkeypatch, tmp_path, mutation):
    """Catches path replacement and in-place byte mutation after OpenTofu opens the plan."""
    plan_path = tmp_path / "saved.tfplan"
    plan_path.write_bytes(b"exact opaque saved plan bytes")

    def change_plan() -> None:
        if mutation == "replace":
            replacement = tmp_path / "replacement.tfplan"
            replacement.write_bytes(plan_path.read_bytes())
            os.replace(replacement, plan_path)
        else:
            plan_path.write_bytes(b"mutated opaque saved plan byte")

    with pytest.raises(UbitofuError):
        _run_check(
            monkeypatch,
            tmp_path,
            plan_path=plan_path,
            during_show=change_plan,
        )


@pytest.mark.parametrize(
    ("base", "desired", "plan_live", "actions", "reason"),
    [
        (10, 10, 10, ("no-op",), "no_change"),
        (10, 20, 10, ("update",), "code_only_change"),
        (10, 20, 20, ("update",), "concurrent_change_converged"),
    ],
)
def test_check_allows_safe_saved_plan_semantics(
    monkeypatch, tmp_path, base, desired, plan_live, actions, reason
):
    """Catches safe no-op, declared, or converged plans being rejected."""
    common = {"id": "synthetic-id", "name": "synthetic"}
    document = _plan_document(
        base={**common, "vlan": base},
        desired={**common, "vlan": desired},
        plan_live={**common, "vlan": plan_live},
        actions=actions,
    )
    outcome, *_ = _run_check(
        monkeypatch,
        tmp_path,
        document=document,
        fresh={"_id": "synthetic-id", "name": "synthetic", "vlan": plan_live},
    )

    assert outcome.blocked is False
    assert "plan_allowed" in _reason_codes(outcome)
    assert reason in _reason_codes(outcome)


def test_check_allows_independent_ui_and_hcl_changes_already_merged_in_plan(
    monkeypatch, tmp_path
):
    """Catches a field-disjoint merged plan being mistaken for a same-field conflict."""
    document = _plan_document(
        base={"id": "synthetic-id", "name": "old", "vlan": 10},
        desired={"id": "synthetic-id", "name": "new", "vlan": 20},
        plan_live={"id": "synthetic-id", "name": "new", "vlan": 10},
        actions=("update",),
    )
    outcome, *_ = _run_check(
        monkeypatch,
        tmp_path,
        document=document,
        fresh={"_id": "synthetic-id", "name": "new", "vlan": 10},
    )

    assert outcome.blocked is False
    assert "plan_allowed" in _reason_codes(outcome)


def test_check_blocks_uncaptured_plan_time_live_drift(monkeypatch, tmp_path):
    """Catches authorizing a plan whose after values would overwrite a UI change."""
    document = _plan_document(
        base={"id": "synthetic-id", "name": "old", "vlan": 10},
        desired={"id": "synthetic-id", "name": "old", "vlan": 20},
        plan_live={"id": "synthetic-id", "name": "new", "vlan": 10},
        actions=("update",),
    )
    outcome, *_ = _run_check(
        monkeypatch,
        tmp_path,
        document=document,
        fresh={"_id": "synthetic-id", "name": "new", "vlan": 10},
    )

    assert outcome.blocked is True
    assert {"source_ownership_ambiguous", "unsafe_plan"} <= _reason_codes(outcome)


def test_check_blocks_same_field_conflict(monkeypatch, tmp_path):
    """Catches authorizing divergent desired and controller changes to one value."""
    common = {"id": "synthetic-id", "name": "synthetic"}
    document = _plan_document(
        base={**common, "vlan": 10},
        desired={**common, "vlan": 20},
        plan_live={**common, "vlan": 30},
        actions=("update",),
    )
    outcome, *_ = _run_check(
        monkeypatch,
        tmp_path,
        document=document,
        fresh={"_id": "synthetic-id", "name": "synthetic", "vlan": 30},
    )

    assert outcome.blocked is True
    assert {"concurrent_value_conflict", "unsafe_plan"} <= _reason_codes(outcome)


def test_check_blocks_stale_controller_after_saved_plan(monkeypatch, tmp_path):
    """Catches treating a post-plan UI change as the plan-time observation."""
    outcome, *_ = _run_check(
        monkeypatch,
        tmp_path,
        fresh={"_id": "synthetic-id", "name": "synthetic", "vlan": 11},
    )

    assert outcome.blocked is True
    assert "stale_controller_observation" in _reason_codes(outcome)


def test_check_compares_optional_computed_provider_value(monkeypatch, tmp_path):
    """Catches settable Optional+Computed values bypassing controller freshness."""
    outcome, *_ = _run_check(
        monkeypatch,
        tmp_path,
        fresh={"_id": "synthetic-id", "name": "synthetic", "vlan": 11},
        schema=_schema(
            vlan_schema={
                "type": "number",
                "optional": True,
                "computed": True,
            }
        ),
    )

    assert outcome.blocked is True
    assert "stale_controller_observation" in _reason_codes(outcome)


def test_check_uses_one_sequential_controller_collection_window(monkeypatch, tmp_path):
    """Catches retrying endpoint reads while claiming one immutable observation."""
    state = {"vlan": 10}
    calls = 0

    def enumerate_once(_controller, *, capture_records):
        nonlocal calls
        calls += 1
        assert capture_records is True
        captured = _enumeration(
            {"_id": "synthetic-id", "name": "synthetic", "vlan": state["vlan"]}
        )
        state["vlan"] = 11
        return captured

    outcome, *_ = _run_check(
        monkeypatch,
        tmp_path,
        enumeration_hook=enumerate_once,
    )

    assert calls == 1
    assert state["vlan"] == 11
    assert outcome.blocked is False


def test_check_blocks_missing_controller_comparison_coverage(monkeypatch, tmp_path):
    """Catches missing managed controller values comparing as unchanged."""
    outcome, *_ = _run_check(
        monkeypatch,
        tmp_path,
        fresh={"_id": "synthetic-id", "name": "synthetic"},
    )

    assert outcome.blocked is True
    assert "incomparable_controller_observation" in _reason_codes(outcome)


def test_check_blocks_delete_versus_controller_modify(monkeypatch, tmp_path):
    """Catches a saved deletion erasing a resource modified through the UI."""
    base = {"id": "synthetic-id", "name": "synthetic", "vlan": 10}
    document = _plan_document(
        base=base,
        desired=None,
        plan_live={**base, "vlan": 11},
        actions=("delete",),
    )
    outcome, *_ = _run_check(
        monkeypatch,
        tmp_path,
        document=document,
        fresh={"_id": "synthetic-id", "name": "synthetic", "vlan": 11},
        include_resource=False,
    )

    assert outcome.blocked is True
    assert "delete_modify_conflict" in _reason_codes(outcome)


def test_check_blocks_unsupported_address(monkeypatch, tmp_path):
    """Catches a legal module address bypassing source-ownership limits."""
    values = {"id": "synthetic-id", "name": "synthetic", "vlan": 10}
    document = _plan_document(
        base=values,
        desired=values,
        plan_live=values,
        module_address="module.edge",
    )
    outcome, *_ = _run_check(monkeypatch, tmp_path, document=document)

    assert outcome.blocked is True
    assert "unsupported_address" in _reason_codes(outcome)


def test_check_blocks_provider_unknown(monkeypatch, tmp_path):
    """Catches authorizing a saved plan with an unevaluated managed value."""
    values = {"id": "synthetic-id", "name": "synthetic", "vlan": 10}
    document = _plan_document(
        base=values,
        desired=values,
        plan_live=values,
        after_unknown={"vlan": True},
    )
    outcome, *_ = _run_check(monkeypatch, tmp_path, document=document)

    assert outcome.blocked is True
    assert {"computed_or_unknown", "unsafe_plan"} <= _reason_codes(outcome)


def test_check_rejects_errored_plan(monkeypatch, tmp_path):
    """Catches an OpenTofu error document becoming an authorization result."""
    document = _plan_document(errored=True)

    with pytest.raises(ExternalDocumentError):
        _run_check(monkeypatch, tmp_path, document=document)


def test_check_preserves_high_risk_pending_create_warning(monkeypatch, tmp_path):
    """Catches a non-device planned create losing its explicit warning."""
    desired = {"id": "synthetic-id", "name": "synthetic", "vlan": 10}
    document = _plan_document(
        base=None,
        desired=desired,
        plan_live=None,
        actions=("create",),
    )
    outcome, *_ = _run_check(
        monkeypatch,
        tmp_path,
        document=document,
        fresh=None,
    )

    assert outcome.blocked is False
    assert {"pending_create", "plan_allowed"} <= _reason_codes(outcome)


def test_check_blocks_forbidden_managed_device_create(monkeypatch, tmp_path):
    """Catches allowing a managed device to be created outside the controller UI."""
    desired = {"mac": "02:00:00:00:00:01", "name": "switch", "vlan": 10}
    document = _plan_document(
        base=None,
        desired=desired,
        plan_live=None,
        actions=("create",),
        resource_type="unifi_device",
        name="switch",
    )
    outcome, *_ = _run_check(
        monkeypatch,
        tmp_path,
        document=document,
        fresh=None,
        resource_type="unifi_device",
        name="switch",
    )

    assert outcome.blocked is True
    assert "forbidden_device_create" in _reason_codes(outcome)
