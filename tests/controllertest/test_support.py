# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 James Braid
"""Unit tests for support.py helpers that don't need docker (unmarked)."""
import pytest

from .support import SEEDED, _report_keep


def test_report_keep_warns_instead_of_printing(capsys):
    # UNIFI_TEST_KEEP's notice used to be a bare print(), which pytest
    # capture swallows unless a run passes -s — the operator would leave
    # a container running with no visible confirmation. warnings.warn
    # lands in the warnings summary unconditionally.
    with pytest.warns(UserWarning, match="UNIFI_TEST_KEEP"):
        _report_keep(SEEDED, "https://127.0.0.1:12345")
    # Nothing goes to stdout/stderr any more — the warning is the only signal.
    captured = capsys.readouterr()
    assert captured.out == ""


def test_report_keep_names_flavor_and_base_url():
    with pytest.warns(UserWarning) as record:
        _report_keep(SEEDED, "https://127.0.0.1:12345")
    assert len(record) == 1
    message = str(record[0].message)
    assert "seeded" in message
    assert "https://127.0.0.1:12345" in message
