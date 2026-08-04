# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 James Braid
"""Tests for stable command outcomes and private receipt output."""

from __future__ import annotations

import io
import json
import os
import stat
from pathlib import Path, PurePosixPath

import pytest

from ubitofu.errors import UbitofuError
from ubitofu.values import freeze_value


def _outcome(*, blocked: bool = False, items=(), digests=None, payload=None):
    from ubitofu.outcomes import CommandOutcome

    return CommandOutcome(
        command="reconcile",
        changed=True,
        blocked=blocked,
        summary="reconciliation complete",
        items=tuple(items),
        input_digests=(
            (("active_source", "a" * 64), ("controller", "b" * 64))
            if digests is None
            else tuple(digests)
        ),
        payload=(
            freeze_value(
                {
                    "changed_paths": ["main.tf"],
                    "candidate_digests": [["main.tf", "a" * 64]],
                }
            )
            if payload is None
            else freeze_value(payload)
        ),
    )


def test_receipt_is_canonical_deterministic_and_has_one_newline() -> None:
    """Catches JSON output depending on incoming item or digest ordering."""
    from ubitofu.outcomes import OutcomeItem, opaque_reference, render_json

    first = _outcome(
        items=(
            OutcomeItem(
                "captured_change", "info", opaque_reference("z"), "captured controller changes"
            ),
            OutcomeItem("advisory", "warning", opaque_reference("a"), "operator attention advised"),
        )
    )
    second = _outcome(
        items=tuple(reversed(first.items)),
    )
    reversed_digests = _outcome(
        digests=(("controller", "b" * 64), ("active_source", "a" * 64)),
    )

    rendered = render_json(first)

    assert rendered == render_json(second)
    assert render_json(_outcome()) == render_json(reversed_digests)
    assert rendered.endswith(b"\n")
    assert not rendered.endswith(b"\n\n")
    assert (
        rendered
        == json.dumps(
            json.loads(rendered), sort_keys=True, separators=(",", ":"), ensure_ascii=True
        ).encode("ascii")
        + b"\n"
    )
    document = json.loads(rendered)
    assert document["schema"] == "dev.ubitofu.receipt"
    assert document["version"] == 1
    assert [item["reason_code"] for item in document["outcome"]["items"]] == [
        "advisory",
        "captured_change",
    ]


@pytest.mark.parametrize(
    "summary,reason,message,payload",
    [
        (
            "password abc123",
            "advisory",
            "operator attention advised",
            {"changed_paths": ["main.tf"]},
        ),
        ("reconciliation complete", "advisory", "password abc123", {"changed_paths": ["main.tf"]}),
        (
            "reconciliation complete",
            "advisory",
            'resource "unifi_network" "lan" {}',
            {"changed_paths": ["main.tf"]},
        ),
        (
            "reconciliation complete",
            "advisory",
            "stderr password abc123",
            {"changed_paths": ["main.tf"]},
        ),
        (
            "reconciliation complete",
            "advisory",
            '{"meta":{"rc":"ok"},"data":{"password":"abc123"}}',
            {"password": "abc123"},
        ),
        (
            "reconciliation complete",
            "advisory",
            "operator attention advised",
            {"changed_paths": ["abc123"]},
        ),
    ],
)
def test_outcome_rejects_untrusted_safe_character_diagnostics_and_payload(
    summary: str, reason: str, message: str, payload: object
) -> None:
    """Catches a character filter mistaking secrets or raw input for public data."""
    from ubitofu.outcomes import CommandOutcome, OutcomeItem

    with pytest.raises(ValueError):
        CommandOutcome(
            command="reconcile",
            changed=False,
            blocked=False,
            summary=summary,
            items=(OutcomeItem(reason, "warning", None, message),),
            input_digests=(),
            payload=freeze_value(payload),
        )


def test_outcome_rejects_unknown_or_mismatched_public_item_vocabulary() -> None:
    """Catches future callers inventing a public message without extending v1."""
    from ubitofu.outcomes import OutcomeItem

    with pytest.raises(ValueError):
        OutcomeItem("unknown", "warning", None, "operator attention advised")
    with pytest.raises(ValueError):
        OutcomeItem("advisory", "info", None, "operator attention advised")
    with pytest.raises(ValueError):
        OutcomeItem("advisory", "warning", None, "different message")


def test_outcome_items_use_only_opaque_public_references() -> None:
    """Catches resource keys, names, and controller text entering a receipt."""
    from ubitofu.outcomes import OutcomeItem, opaque_reference

    reference = opaque_reference('unifi_network.a["secret"]')
    assert reference == opaque_reference('unifi_network.a["secret"]')
    assert reference != opaque_reference("password.value")
    assert reference.startswith("ref-")
    assert len(reference) == 68
    assert OutcomeItem("captured_change", "info", reference, "captured controller changes")
    for raw in ('unifi_network.a["secret"]', "password.value", '{"password":"abc123"}'):
        with pytest.raises(ValueError):
            OutcomeItem("captured_change", "info", raw, "captured controller changes")


def test_command_profiles_accept_only_their_declared_schema() -> None:
    """Catches later commands bypassing the v1 profile registry."""
    from ubitofu import outcomes
    from ubitofu.outcomes import OutcomeItem

    profiles = {
        "generate": (
            "generation preview complete",
            (("active_source", "a" * 64), ("controller", "b" * 64), ("provider_schema", "c" * 64)),
            _preview_payload(),
        ),
        "reconcile": (
            "reconciliation complete",
            (("active_source", "a" * 64), ("controller", "b" * 64)),
            _preview_payload(),
        ),
        "check": (
            "saved plan check complete",
            (
                ("saved_plan", "a" * 64),
                ("active_source", "b" * 64),
                ("plan_time_live", "c" * 64),
                ("fresh_controller", "d" * 64),
            ),
            None,
        ),
        "inspect": (
            "inspection complete",
            (("controller", "a" * 64), ("provider_schema", "b" * 64)),
            None,
        ),
        "health_snapshot": (
            "health snapshot complete",
            (("controller", "a" * 64),),
            _health_payload(),
        ),
        "health_compare": (
            "health comparison complete",
            (("health_before", "a" * 64), ("health_after", "b" * 64)),
            _health_payload(),
        ),
    }

    assert set(outcomes.COMMAND_PROFILES) == set(profiles)
    for command, (summary, digests, payload) in profiles.items():
        outcome = outcomes.CommandOutcome(
            command, False, False, summary, (), digests, freeze_value(payload)
        )
        assert outcome.command == command
    with pytest.raises(ValueError):
        outcomes.CommandOutcome(
            "check",
            False,
            False,
            "saved plan check complete",
            (OutcomeItem("captured_change", "info", None, "captured controller changes"),),
            profiles["check"][1],
            None,
        )


def test_preview_and_health_payloads_are_canonical_unique_and_closed() -> None:
    """Catches receipt payloads drifting from deterministic public identities."""
    from ubitofu.outcomes import CommandOutcome, opaque_reference

    preview = CommandOutcome(
        "reconcile",
        True,
        False,
        "reconciliation complete",
        (),
        (("active_source", "a" * 64), ("controller", "b" * 64)),
        freeze_value(
            {
                "changed_paths": ["z.tf", "a.tf"],
                "candidate_digests": [["z.tf", None], ["a.tf", "c" * 64]],
            }
        ),
    )
    assert preview.payload == freeze_value(
        {
            "changed_paths": ["a.tf", "z.tf"],
            "candidate_digests": [["a.tf", "c" * 64], ["z.tf", None]],
        }
    )
    with pytest.raises(ValueError):
        CommandOutcome(
            "reconcile",
            True,
            False,
            "reconciliation complete",
            (),
            (("active_source", "a" * 64), ("controller", "b" * 64)),
            freeze_value({"changed_paths": ["a.tf", "a.tf"], "candidate_digests": []}),
        )

    first = opaque_reference("network")
    second = opaque_reference("gateway")
    health = CommandOutcome(
        "health_snapshot",
        False,
        False,
        "health snapshot complete",
        (),
        (("controller", "a" * 64),),
        freeze_value(
            {
                "subsystems": [
                    {"ref": second, "status": "warning", "rank": 1},
                    {"ref": first, "status": "ok", "rank": 0},
                ]
            }
        ),
    )
    assert health.payload == freeze_value(
        {
            "subsystems": [
                {"ref": first, "status": "ok", "rank": 0},
                {"ref": second, "status": "warning", "rank": 1},
            ]
        }
    )
    with pytest.raises(ValueError):
        CommandOutcome(
            "health_snapshot",
            False,
            False,
            "health snapshot complete",
            (),
            (("controller", "a" * 64),),
            freeze_value({"subsystems": [{"ref": first, "status": "ok", "rank": 0}] * 2}),
        )
    with pytest.raises(ValueError):
        CommandOutcome(
            "health_snapshot",
            False,
            False,
            "health snapshot complete",
            (),
            (("controller", "a" * 64),),
            freeze_value({"subsystems": [{"ref": 7, "status": "ok", "rank": 0}]}),
        )


def test_only_generate_preview_can_include_exact_coverage_markdown() -> None:
    from ubitofu.outcomes import CommandOutcome

    digests = (
        ("active_source", "a" * 64),
        ("controller", "b" * 64),
        ("provider_schema", "c" * 64),
    )
    payload = freeze_value(
        {
            "changed_paths": ["COVERAGE.md", "generated.tf"],
            "candidate_digests": [
                ["COVERAGE.md", "d" * 64],
                ["generated.tf", "e" * 64],
            ],
        }
    )

    outcome = CommandOutcome(
        "generate", True, False, "generation preview complete", (), digests, payload
    )
    assert outcome.payload == payload

    with pytest.raises(ValueError):
        CommandOutcome(
            "reconcile",
            True,
            False,
            "reconciliation complete",
            (),
            (("active_source", "a" * 64), ("controller", "b" * 64)),
            payload,
        )
    for markdown in ("README.md", "nested/COVERAGE.md"):
        with pytest.raises(ValueError):
            CommandOutcome(
                "generate",
                True,
                False,
                "generation preview complete",
                (),
                digests,
                freeze_value(
                    {
                        "changed_paths": [markdown],
                        "candidate_digests": [[markdown, "d" * 64]],
                    }
                ),
            )


@pytest.mark.parametrize(
    ("command", "summary", "digests"),
    [
        (
            "generate",
            "generation preview complete",
            (
                ("active_source", "a" * 64),
                ("controller", "b" * 64),
                ("provider_schema", "c" * 64),
            ),
        ),
        (
            "reconcile",
            "reconciliation complete",
            (("active_source", "a" * 64), ("controller", "b" * 64)),
        ),
    ],
)
def test_human_preview_outcomes_render_only_canonical_changed_paths(
    command: str, summary: str, digests: tuple[tuple[str, str], ...]
) -> None:
    """Catches dry-run output omitting files or leaking candidate metadata."""
    from ubitofu.outcomes import CommandOutcome, render_human

    outcome = CommandOutcome(
        command,
        True,
        False,
        summary,
        (),
        digests,
        freeze_value(
            {
                "changed_paths": ["z.tf", "a.tf"],
                "candidate_digests": [["z.tf", "d" * 64], ["a.tf", "e" * 64]],
            }
        ),
    )

    assert render_human(outcome) == f"{summary}\nChanged paths:\n  a.tf\n  z.tf\n"
    assert "d" * 64 not in render_human(outcome)
    assert "e" * 64 not in render_human(outcome)


def test_human_nonpreview_outcomes_do_not_render_changed_paths() -> None:
    """Catches generic human rendering inventing a preview section for other commands."""
    from ubitofu.outcomes import CommandOutcome, render_human

    outcome = CommandOutcome(
        "check",
        False,
        False,
        "saved plan check complete",
        (),
        (
            ("saved_plan", "a" * 64),
            ("active_source", "b" * 64),
            ("plan_time_live", "c" * 64),
            ("fresh_controller", "d" * 64),
        ),
        None,
    )

    assert render_human(outcome) == "saved plan check complete\n"


def test_blocking_item_and_outcome_flags_agree_both_ways() -> None:
    """Catches a receipt emitting a blocking reason with a success exit code."""
    from ubitofu.outcomes import OutcomeItem

    blocking = OutcomeItem("reconciliation_blocked", "blocking", None, "reconciliation blocked")
    with pytest.raises(ValueError):
        _outcome(blocked=False, items=(blocking,))
    with pytest.raises(ValueError):
        _outcome(blocked=True)
    assert _outcome(
        blocked=False,
        items=(OutcomeItem("advisory", "warning", None, "operator attention advised"),),
    )
    assert _outcome(blocked=True, items=(blocking,))


def _preview_payload() -> dict[str, object]:
    return {
        "changed_paths": ["main.tf"],
        "candidate_digests": [["main.tf", "a" * 64]],
    }


def _health_payload() -> dict[str, object]:
    from ubitofu.outcomes import opaque_reference

    return {"subsystems": [{"ref": opaque_reference("network"), "status": "ok", "rank": 0}]}


@pytest.mark.parametrize("blocked,expected", [(False, 0), (True, 3)])
def test_domain_exit_code_is_only_success_or_blocking(blocked: bool, expected: int) -> None:
    """Catches receipt rendering growing command-specific status codes."""
    from ubitofu.outcomes import OutcomeItem, exit_code

    item = (
        OutcomeItem("reconciliation_blocked", "blocking", None, "reconciliation blocked")
        if blocked
        else OutcomeItem("advisory", "warning", None, "operator attention advised")
    )
    assert exit_code(_outcome(blocked=blocked, items=(item,))) == expected


def test_decode_receipt_v1_requires_contract_fields_and_ignores_unknown_optional() -> None:
    """Catches accidental breaking changes to version-one receipt readers."""
    from ubitofu.outcomes import ReceiptEnvelope, decode_receipt, render_json

    document = json.loads(render_json(_outcome()))
    document["new_optional_field"] = {"future": True}
    document["outcome"]["future_note"] = "ignored"

    decoded = decode_receipt(json.dumps(document).encode())

    assert isinstance(decoded, ReceiptEnvelope)
    assert decoded.outcome.command == "reconcile"
    del document["outcome"]["payload"]
    with pytest.raises(UbitofuError):
        decode_receipt(json.dumps(document).encode())
    document["outcome"]["payload"] = None
    document["schema"] = "example.invalid"
    with pytest.raises(UbitofuError):
        decode_receipt(json.dumps(document).encode())
    with pytest.raises(UbitofuError):
        decode_receipt(
            b'{"schema":"dev.ubitofu.receipt","schema":"dev.ubitofu.receipt","version":1}'
        )


def test_decode_receipt_rejects_oversized_and_deep_documents() -> None:
    """Catches an untrusted receipt consuming unbounded parser memory or depth."""
    from ubitofu.outcomes import decode_receipt, render_json

    with pytest.raises(UbitofuError):
        decode_receipt(b"{" + b"x" * (128 * 1024) + b"}")
    deeply_nested = "[" * 40 + "0" + "]" * 40
    with pytest.raises(UbitofuError):
        decode_receipt(deeply_nested.encode())
    document = json.loads(render_json(_outcome()))
    document["future"] = "x" * 241
    with pytest.raises(UbitofuError):
        decode_receipt(json.dumps(document).encode())
    too_many_values = b"[" + b",".join(b"0" for _ in range(257)) + b"]"
    with pytest.raises(UbitofuError):
        decode_receipt(too_many_values)


def test_source_digest_is_order_independent_and_length_prefixed() -> None:
    """Catches ambiguous concatenation of path and source bytes."""
    from ubitofu.outcomes import digest_active_source

    first = ((PurePosixPath("a"), b"bc"), (PurePosixPath("d"), b"ef"))
    reordered = tuple(reversed(first))
    ambiguous = ((PurePosixPath("ab"), b"c"), (PurePosixPath("d"), b"ef"))

    assert digest_active_source(first) == digest_active_source(reordered)
    assert digest_active_source(first) != digest_active_source(ambiguous)
    for duplicate in (
        ((PurePosixPath("a"), b"first"), (PurePosixPath("a"), b"first")),
        ((PurePosixPath("a"), b"first"), (PurePosixPath("a"), b"second")),
    ):
        with pytest.raises(ValueError):
            digest_active_source(duplicate)


def test_controller_digest_is_order_independent_and_length_prefixed() -> None:
    """Catches ambiguous concatenation of normalized controller observations."""
    from ubitofu.outcomes import digest_controller_observations

    first = (("a", freeze_value({"value": "bc"})), ("d", freeze_value({"value": "ef"})))
    reordered = tuple(reversed(first))
    ambiguous = (("ab", freeze_value({"value": "c"})), ("d", freeze_value({"value": "ef"})))

    assert digest_controller_observations(first) == digest_controller_observations(reordered)
    assert digest_controller_observations(first) != digest_controller_observations(ambiguous)
    for duplicate in (
        (("unifi_network.lan", freeze_value({"vlan": 10})),) * 2,
        (
            ("unifi_network.lan", freeze_value({"vlan": 10})),
            ("unifi_network.lan", freeze_value({"vlan": 20})),
        ),
    ):
        with pytest.raises(ValueError):
            digest_controller_observations(duplicate)


def test_emit_output_writes_human_or_json_to_stdout() -> None:
    """Catches output mode re-classifying or serializing a second outcome."""
    from ubitofu.outcomes import emit_output, render_human, render_json

    outcome = _outcome()
    human_stdout = io.StringIO()
    json_stdout = io.StringIO()

    emit_output(outcome, format="human", output="-", stdout=human_stdout)
    emit_output(outcome, format="json", output="-", stdout=json_stdout)

    assert human_stdout.getvalue() == render_human(outcome)
    assert json_stdout.getvalue().encode("ascii") == render_json(outcome)


@pytest.mark.parametrize("written", [None, 0, 1])
def test_emit_output_rejects_nonexact_stdout_writes(written: int | None) -> None:
    """Catches treating buffered or partial stdout writes as a completed receipt."""
    from ubitofu.outcomes import emit_output

    class ShortStream:
        def write(self, value: str) -> int | None:
            return written

    with pytest.raises(UbitofuError):
        emit_output(_outcome(), format="json", output="-", stdout=ShortStream())


def test_emit_output_creates_or_replaces_a_private_regular_file(tmp_path: Path) -> None:
    """Catches receipt files inheriting public permissions or stale bytes."""
    from ubitofu.outcomes import emit_output, render_json

    destination = tmp_path / "receipt.json"
    outcome = _outcome()
    emit_output(outcome, format="json", output=str(destination), stdout=io.StringIO())
    assert destination.read_bytes() == render_json(outcome)
    assert stat.S_IMODE(destination.lstat().st_mode) == 0o600

    destination.write_text("old", encoding="ascii")
    os.chmod(destination, 0o644)
    emit_output(outcome, format="json", output=str(destination), stdout=io.StringIO())
    assert destination.read_bytes() == render_json(outcome)
    assert stat.S_IMODE(destination.lstat().st_mode) == 0o600


def test_emit_output_rejects_symlink_and_nonregular_destinations_or_parents(tmp_path: Path) -> None:
    """Catches a receipt path redirecting writes outside its requested location."""
    from ubitofu.outcomes import emit_output

    outcome = _outcome()
    target = tmp_path / "target.json"
    target.write_text("target", encoding="ascii")
    destination = tmp_path / "receipt.json"
    destination.symlink_to(target)
    with pytest.raises(UbitofuError):
        emit_output(outcome, format="json", output=str(destination), stdout=io.StringIO())

    fifo = tmp_path / "receipt.fifo"
    os.mkfifo(fifo)
    with pytest.raises(UbitofuError):
        emit_output(outcome, format="json", output=str(fifo), stdout=io.StringIO())

    real_parent = tmp_path / "real"
    real_parent.mkdir()
    symlink_parent = tmp_path / "linked"
    symlink_parent.symlink_to(real_parent, target_is_directory=True)
    with pytest.raises(UbitofuError):
        emit_output(
            outcome,
            format="json",
            output=str(symlink_parent / "receipt.json"),
            stdout=io.StringIO(),
        )
    with pytest.raises(UbitofuError):
        emit_output(
            outcome,
            format="json",
            output=str(symlink_parent / ".." / "escaped.json"),
            stdout=io.StringIO(),
        )


def test_emit_output_rejects_owner_mismatch(tmp_path: Path, monkeypatch) -> None:
    """Catches replacing a receipt file belonging to another account."""
    from ubitofu import outcomes

    destination = tmp_path / "receipt.json"
    destination.write_text("old", encoding="ascii")
    actual_lstat = outcomes.os.lstat

    def foreign_owner(path):
        result = actual_lstat(path)
        if Path(path) == destination:
            return os.stat_result(
                (
                    result.st_mode,
                    result.st_ino,
                    result.st_dev,
                    result.st_nlink,
                    result.st_uid + 1,
                    result.st_gid,
                    result.st_size,
                    result.st_atime,
                    result.st_mtime,
                    result.st_ctime,
                )
            )
        return result

    monkeypatch.setattr(outcomes.os, "lstat", foreign_owner)
    with pytest.raises(UbitofuError):
        outcomes.emit_output(
            _outcome(), format="json", output=str(destination), stdout=io.StringIO()
        )


def test_emit_output_rejects_short_write_and_fsync_failure(tmp_path: Path, monkeypatch) -> None:
    """Catches reporting success after an incomplete or non-durable receipt write."""
    from ubitofu import outcomes

    destination = tmp_path / "receipt.json"
    old = b"old\n"
    destination.write_bytes(old)
    real_write = outcomes.os.write
    monkeypatch.setattr(outcomes.os, "write", lambda fd, value: 0)
    with pytest.raises(UbitofuError):
        outcomes.emit_output(
            _outcome(), format="json", output=str(destination), stdout=io.StringIO()
        )
    assert destination.read_bytes() == old
    monkeypatch.setattr(outcomes.os, "write", real_write)
    monkeypatch.setattr(outcomes.os, "fsync", lambda fd: (_ for _ in ()).throw(OSError("nope")))
    with pytest.raises(UbitofuError):
        outcomes.emit_output(
            _outcome(), format="json", output=str(destination), stdout=io.StringIO()
        )
    assert destination.read_bytes() == old


def test_emit_output_rechecks_destination_before_replace_and_cleans_temporary(
    tmp_path: Path, monkeypatch
) -> None:
    """Catches an editor replacing the output after its initial validation."""
    from ubitofu import outcomes

    destination = tmp_path / "receipt.json"
    destination.write_bytes(b"old\n")
    original_write_all = outcomes._write_all

    def replace_destination(fd: int, content: bytes) -> None:
        original_write_all(fd, content)
        replacement = tmp_path / "replacement.json"
        replacement.write_bytes(b"newer editor output\n")
        os.replace(replacement, destination)

    monkeypatch.setattr(outcomes, "_write_all", replace_destination)
    with pytest.raises(UbitofuError):
        outcomes.emit_output(
            _outcome(), format="json", output=str(destination), stdout=io.StringIO()
        )
    assert destination.read_bytes() == b"newer editor output\n"
    assert list(tmp_path.glob(".receipt.json.ubitofu-*.tmp")) == []


def test_emit_output_reports_directory_fsync_failure_after_replace(
    tmp_path: Path, monkeypatch
) -> None:
    """Catches claiming receipt durability when replacement reached the directory only."""
    from ubitofu import outcomes

    destination = tmp_path / "receipt.json"
    monkeypatch.setattr(
        outcomes,
        "_fsync_directory",
        lambda path: (_ for _ in ()).throw(OSError("synthetic directory fsync failure")),
    )
    with pytest.raises(UbitofuError):
        outcomes.emit_output(
            _outcome(), format="json", output=str(destination), stdout=io.StringIO()
        )
    assert destination.read_bytes() == outcomes.render_json(_outcome())
