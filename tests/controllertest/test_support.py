# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 James Braid
"""Unit tests for support.py helpers that don't need docker (unmarked)."""
import pytest

from .support import SEEDED, UOS_RUN_KWARGS, _new_container, _report_keep


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


# The regression imports the optional controller-test dependency but does not
# start a daemon or container.
@pytest.mark.controller
def test_uos_tmpfs_uses_testcontainers_mount_api_without_duplicate_create_kwarg(
    monkeypatch,
):
    from testcontainers.core import container as container_module

    constructor_calls = []
    mounts = []

    class RecordingContainer:
        def __init__(self, image, **kwargs):
            constructor_calls.append((image, kwargs))

        def with_tmpfs_mount(self, path, options=None):
            mounts.append((path, options))
            return self

    monkeypatch.setattr(container_module, "DockerContainer", RecordingContainer)

    container = _new_container("synthetic/uos:pin", UOS_RUN_KWARGS)

    assert isinstance(container, RecordingContainer)
    assert constructor_calls == [(
        "synthetic/uos:pin",
        {key: value for key, value in UOS_RUN_KWARGS.items() if key != "tmpfs"},
    )]
    assert mounts == list(UOS_RUN_KWARGS["tmpfs"].items())
    assert "tmpfs" in UOS_RUN_KWARGS
