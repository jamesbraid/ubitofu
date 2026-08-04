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


def _outcome(*, blocked: bool = False, items=(), digests=None):
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
        payload=freeze_value({"paths": ["main.tf"]}),
    )


def test_receipt_is_canonical_deterministic_and_has_one_newline() -> None:
    """Catches JSON output depending on incoming item or digest ordering."""
    from ubitofu.outcomes import OutcomeItem, render_json

    first = _outcome(
        items=(
            OutcomeItem("zeta", "warning", "unifi_network.z", "late"),
            OutcomeItem("alpha", "info", "unifi_network.a", "early"),
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
    assert rendered == json.dumps(
        json.loads(rendered), sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("ascii") + b"\n"
    document = json.loads(rendered)
    assert document["schema"] == "dev.ubitofu.receipt"
    assert document["version"] == 1
    assert [item["reason_code"] for item in document["outcome"]["items"]] == [
        "alpha",
        "zeta",
    ]


def test_human_and_json_render_only_one_sanitized_immutable_outcome() -> None:
    """Catches diagnostics leaking arbitrary controller or plan text."""
    from ubitofu.outcomes import CommandOutcome, OutcomeItem, render_human, render_json

    secret = "token=super-secret-value\n" + "x" * 500
    outcome = CommandOutcome(
        command="reconcile",
        changed=False,
        blocked=False,
        summary=secret,
        items=(OutcomeItem("bad reason!", "warning", "bad address!", secret),),
        input_digests=(),
        payload=None,
    )

    human = render_human(outcome)
    rendered = render_json(outcome).decode("ascii")

    assert "super-secret-value" not in human
    assert "super-secret-value" not in rendered
    assert "details redacted" in human
    assert "details redacted" in rendered
    assert human.endswith("\n")


@pytest.mark.parametrize("blocked,expected", [(False, 0), (True, 3)])
def test_domain_exit_code_is_only_success_or_blocking(blocked: bool, expected: int) -> None:
    """Catches receipt rendering growing command-specific status codes."""
    from ubitofu.outcomes import OutcomeItem, exit_code

    warning = OutcomeItem("advisory", "warning", None, "operator attention advised")
    assert exit_code(_outcome(blocked=blocked, items=(warning,))) == expected


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


def test_source_digest_is_order_independent_and_length_prefixed() -> None:
    """Catches ambiguous concatenation of path and source bytes."""
    from ubitofu.outcomes import digest_active_source

    first = ((PurePosixPath("a"), b"bc"), (PurePosixPath("d"), b"ef"))
    reordered = tuple(reversed(first))
    ambiguous = ((PurePosixPath("ab"), b"c"), (PurePosixPath("d"), b"ef"))

    assert digest_active_source(first) == digest_active_source(reordered)
    assert digest_active_source(first) != digest_active_source(ambiguous)


def test_controller_digest_is_order_independent_and_length_prefixed() -> None:
    """Catches ambiguous concatenation of normalized controller observations."""
    from ubitofu.outcomes import digest_controller_observations

    first = (("a", freeze_value({"value": "bc"})), ("d", freeze_value({"value": "ef"})))
    reordered = tuple(reversed(first))
    ambiguous = (("ab", freeze_value({"value": "c"})), ("d", freeze_value({"value": "ef"})))

    assert digest_controller_observations(first) == digest_controller_observations(reordered)
    assert digest_controller_observations(first) != digest_controller_observations(ambiguous)


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


def test_emit_output_rejects_owner_mismatch(tmp_path: Path, monkeypatch) -> None:
    """Catches replacing a receipt file belonging to another account."""
    from ubitofu import outcomes

    destination = tmp_path / "receipt.json"
    destination.write_text("old", encoding="ascii")
    actual_lstat = outcomes.os.lstat

    def foreign_owner(path):
        result = actual_lstat(path)
        if Path(path) == destination:
            return os.stat_result((
                result.st_mode, result.st_ino, result.st_dev, result.st_nlink,
                result.st_uid + 1, result.st_gid, result.st_size,
                result.st_atime, result.st_mtime, result.st_ctime,
            ))
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
