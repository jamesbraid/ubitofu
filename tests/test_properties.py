# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 James Braid
"""Property tests for structural patches, planning, and slug assignment."""

from pathlib import PurePosixPath

from hypothesis import given, settings
from hypothesis import strategies as st

from ubitofu.enumerator import ImportTarget
from ubitofu.hcl_index import ByteSpan
from ubitofu.hcl_patches import BytePatch, apply_patches
from ubitofu.import_emitter import assign_slugs
from ubitofu.module_index import ModuleIndex
from ubitofu.reconcile_model import (
    ActionVector,
    Disposition,
    FileIdentity,
    LifecyclePolicy,
    ReasonCode,
    ReconcileSnapshot,
    ResourceChange,
    ResourceObservation,
    SourceResource,
    parse_opentofu_address,
)
from ubitofu.reconcile_planner import build_reconcile_plan
from ubitofu.values import FrozenObject, freeze_value


@given(
    names=st.lists(
        st.text(
            alphabet=st.characters(
                categories=(),
                include_characters="abcdefghijklmnopqrstuvwxyz0123456789_ ",
            ),
            min_size=1,
            max_size=20,
        ),
        min_size=1,
        max_size=8,
    ),
    # Reserved slugs must come from the same [a-z][a-z0-9_]* space that
    # slugify() produces.  Full-Unicode reserved values can never equal a
    # generated slug, making the reserved-avoidance assertion vacuously true.
    reserved=st.sets(
        st.from_regex(r"[a-z][a-z0-9_]*", fullmatch=True).map(lambda s: s[:25]),
        max_size=5,
    ),
)
def test_assign_slugs_never_collides_with_reserved_or_itself(names, reserved):
    targets = [ImportTarget("unifi_device", n, f"mac{i}") for i, n in enumerate(names)]
    res = {f"unifi_device.{r}" for r in reserved}
    out = assign_slugs(targets, reserved=res)
    slugs = [s for _, s in out]
    assert len(set(slugs)) == len(slugs)                        # intra-batch unique
    assert all(f"unifi_device.{s}" not in res for s in slugs)  # never reuse reserved


@given(
    source=st.binary(min_size=1, max_size=80),
    start=st.data(),
)
@settings(max_examples=200)
def test_byte_patches_preserve_bytes_outside_selected_spans(source, start):
    """Catches patch application that alters bytes outside a selected replacement."""
    left = start.draw(st.integers(min_value=0, max_value=len(source)))
    right = start.draw(st.integers(min_value=left, max_value=len(source)))
    patch = BytePatch(
        span=ByteSpan(left, right),
        expected=source[left:right],
        replacement=b"replacement",
        reason="property",
    )

    assert apply_patches(source, (patch,)) == source[:left] + b"replacement" + source[right:]


@given(
    source=st.binary(min_size=2, max_size=80),
    data=st.data(),
)
@settings(max_examples=200)
def test_byte_patch_result_is_independent_of_input_order(source, data):
    """Catches mutations whose result changes with an otherwise equivalent patch order."""
    first_end = data.draw(st.integers(min_value=1, max_value=len(source) - 1))
    first_start = data.draw(st.integers(min_value=0, max_value=first_end - 1))
    second_start = data.draw(st.integers(min_value=first_end, max_value=len(source)))
    second_end = data.draw(st.integers(min_value=second_start, max_value=len(source)))
    first = BytePatch(
        ByteSpan(first_start, first_end), source[first_start:first_end], b"A", "first"
    )
    second = BytePatch(
        ByteSpan(second_start, second_end), source[second_start:second_end], b"B", "second"
    )

    assert apply_patches(source, (first, second)) == apply_patches(source, (second, first))


def _planner_observation(value, *, suffix="one", blockers=()):
    address = parse_opentofu_address(f"unifi_network.{suffix}")
    base = freeze_value({"vlan": value})
    desired = freeze_value({"vlan": value})
    live = freeze_value({"vlan": value + 1})
    unknown = freeze_value({})
    assert all(isinstance(item, FrozenObject) for item in (base, desired, live, unknown))
    identity = FileIdentity(PurePosixPath(f"{suffix}.tf"), 1, 2, 3, 4, 5, 6, 7, "a" * 64)
    source = SourceResource(address, identity, b"source", desired)
    change = ResourceChange(address, ActionVector.UPDATE, live, desired, unknown)
    return ResourceObservation(
        address,
        source,
        base,
        desired,
        live,
        change,
        LifecyclePolicy(False, "attention"),
        (),
        blockers,
    )


def _planner_snapshot(*observations):
    return ReconcileSnapshot(
        tuple(observations), ModuleIndex((), (), (), (), ()), (), "digest"
    )


@given(value=st.integers(min_value=-1000, max_value=1000))
def test_reconcile_planner_is_total_over_valid_scalar_observations(value):
    plan = build_reconcile_plan(_planner_snapshot(_planner_observation(value)))

    assert len(plan.decisions) == 1


@given(first=st.integers(), second=st.integers())
def test_reconcile_planner_is_deterministic_and_observation_order_independent(first, second):
    one = _planner_observation(first, suffix="one")
    two = _planner_observation(second, suffix="two")

    assert build_reconcile_plan(_planner_snapshot(one, two)) == build_reconcile_plan(
        _planner_snapshot(two, one)
    )


def test_blocker_order_does_not_change_deterministic_plan():
    reasons = (
        ReasonCode.STALE_CONTROLLER_OBSERVATION,
        ReasonCode.INCOMPARABLE_CONTROLLER_OBSERVATION,
    )

    left = build_reconcile_plan(
        _planner_snapshot(_planner_observation(1, blockers=reasons))
    )
    right = build_reconcile_plan(
        _planner_snapshot(_planner_observation(1, blockers=tuple(reversed(reasons))))
    )

    assert left == right


@given(values=st.lists(st.integers(), min_size=1, max_size=8, unique=True))
def test_reconcile_plan_has_one_decision_per_observation_and_unique_edit_anchors(values):
    observations = tuple(
        _planner_observation(value, suffix=f"r{index}") for index, value in enumerate(values)
    )
    plan = build_reconcile_plan(_planner_snapshot(*observations))
    anchors = [edit.anchor for edit in plan.edits if hasattr(edit, "anchor")]

    assert len(plan.decisions) == len(observations)
    assert len(anchors) == len(set(anchors))


def test_forbidden_decision_is_never_reported_as_pending():
    observation = _planner_observation(1)
    create = ResourceChange(
        observation.address,
        ActionVector.CREATE,
        None,
        observation.desired,
        freeze_value({}),
    )
    forbidden = ResourceObservation(
        observation.address,
        observation.committed,
        None,
        observation.desired,
        None,
        create,
        LifecyclePolicy(True, "forbid"),
        (),
    )

    decision = build_reconcile_plan(_planner_snapshot(forbidden)).decisions[0]

    assert decision.disposition is Disposition.FORBIDDEN
    assert decision.reason is ReasonCode.FORBIDDEN_DEVICE_CREATE


def test_any_blocking_decision_suppresses_every_edit():
    editable = _planner_observation(1, suffix="editable")
    blocked = _planner_observation(
        2,
        suffix="blocked",
        blockers=(ReasonCode.INCOMPARABLE_CONTROLLER_OBSERVATION,),
    )

    plan = build_reconcile_plan(_planner_snapshot(editable, blocked))

    assert plan.blocked is True
    assert plan.edits == ()


def test_build_reconcile_plan_has_no_io_dependency(monkeypatch):
    def fail(*args, **kwargs):
        raise AssertionError("planner attempted I/O")

    monkeypatch.setattr("builtins.open", fail)
    monkeypatch.setattr("pathlib.Path.read_bytes", fail)
    monkeypatch.setattr("pathlib.Path.write_bytes", fail)

    build_reconcile_plan(_planner_snapshot(_planner_observation(1)))
