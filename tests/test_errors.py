# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 James Braid
import re

import pytest

from ubitofu.errors import (
    ControllerResponseError,
    ExternalDocumentError,
    TofuExecutionError,
    render_safe_error,
)


@pytest.mark.parametrize(
    "error",
    [
        TofuExecutionError("plan", 1, "provider stderr api_key=example-secret\\n\\x1b[31m"),
        ExternalDocumentError("plan", "errored", "response body password=example-secret\\t"),
        ControllerResponseError(
            "https://operator:example-secret@example.invalid/x", 401, "raw response"
        ),
        RuntimeError("x" * (100 * 1024)),
    ],
)
def test_render_safe_error_exposes_only_bounded_typed_context(error):
    rendered = render_safe_error(error)

    assert len(rendered) <= 240
    assert "example-secret" not in rendered
    assert "operator" not in rendered
    assert "password" not in rendered
    assert "\n" not in rendered and "\t" not in rendered and "\x1b" not in rendered
    assert not re.search(r"[\x00-\x1f\x7f]", rendered)


def test_typed_errors_retain_their_typed_context():
    error = TofuExecutionError("plan", 2, "execution failed")

    assert (error.command, error.exit_code, error.reason) == ("plan", 2, "execution failed")
