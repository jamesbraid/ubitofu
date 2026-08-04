# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 James Braid
"""Property-based tests for the HCL surgeon and slug-assignment invariants.

These encode the safety contract of hcl_surgeon.py and assign_slugs(), and
are deliberately written against the public interface so they validate any
future implementation (e.g. a tree-sitter backend).
"""

from pathlib import PurePosixPath

import pytest
from hypothesis import assume, given, settings
from hypothesis import strategies as st

from ubitofu.enumerator import ImportTarget
from ubitofu.hcl_index import ByteSpan
from ubitofu.hcl_patches import BytePatch, apply_patches
from ubitofu.hcl_surgeon import _serialize, find_resource_block_span, update_scalar  # noqa: F401
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

# _lit: the canonical scalar-to-HCL-literal helper.  Re-uses the surgeon's own
# _serialize so the property tests speak the surgeon's format, not a hand-rolled
# divergent one.
_lit = _serialize

# Strings in the surgeon's documented domain: no unescaped quotes, no
# backslashes, no newlines, no bare $ (heredocs / ${…} interpolations are
# explicitly out of scope for UniFi scalar values per the module docstring).
_safe_strings = st.text(
    alphabet=st.characters(blacklist_characters='"\\\n$'),
    min_size=0,
    max_size=30,
)

scalar_vals = st.one_of(
    _safe_strings,
    st.integers(min_value=-1000, max_value=100000),
    st.booleans(),
)


# ---------------------------------------------------------------------------
# (a) Idempotence: update_scalar with old == new returns byte-identical text.
# ---------------------------------------------------------------------------

@given(v=scalar_vals)
@settings(max_examples=200)
def test_update_scalar_idempotent_when_old_equals_new(v):
    text = f'resource "unifi_device" "x" {{\n  # keep me\n  name = {_lit(v)}\n}}\n'
    assert update_scalar(text, "unifi_device", "x", "name", v, v) == text


# ---------------------------------------------------------------------------
# (b) Byte-preservation: comments, other attrs, blank lines survive an edit.
# ---------------------------------------------------------------------------

@given(old=scalar_vals, new=scalar_vals)
@settings(max_examples=200)
def test_update_scalar_preserves_comments_and_other_lines(old, new):
    assume(old != new)
    text = (
        'resource "unifi_device" "x" {\n'
        '  # a comment above\n'
        f'  name = {_lit(old)}  # inline\n'
        '  mac  = "aa:bb"\n'
        '\n'
        '  # trailing comment\n'
        '}\n'
    )
    out = update_scalar(text, "unifi_device", "x", "name", old, new)
    # Full-byte equality: the only difference from `text` must be the target
    # line's value.  This subsumes both "target changed" and "everything else
    # preserved" — a substring check can false-pass when _lit(new) happens to
    # coincide with another token already present in the template.
    expected = (
        'resource "unifi_device" "x" {\n'
        '  # a comment above\n'
        f'  name = {_lit(new)}  # inline\n'
        '  mac  = "aa:bb"\n'
        '\n'
        '  # trailing comment\n'
        '}\n'
    )
    assert out == expected


# ---------------------------------------------------------------------------
# (c) Anchor-safety: raises ValueError when committed literal != claimed old.
# ---------------------------------------------------------------------------

@given(new=scalar_vals)
def test_update_scalar_anchor_mismatch_raises(new):
    text = 'resource "unifi_device" "x" {\n  name = "committed"\n}\n'
    with pytest.raises(ValueError):
        update_scalar(text, "unifi_device", "x", "name", "WRONG_OLD", new)


# ---------------------------------------------------------------------------
# (d) Slug uniqueness: assign_slugs never collides intra-batch nor with reserved.
# ---------------------------------------------------------------------------

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
