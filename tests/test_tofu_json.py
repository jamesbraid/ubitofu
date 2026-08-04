# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 James Braid
import pytest

from ubitofu.errors import ExternalDocumentError
from ubitofu.tofu_json import validate_document_header


@pytest.mark.parametrize(
    ("value", "kind", "expected"),
    [
        ({"format_version": "1.0", "errored": False, "future": {"kept": True}}, "plan", (1, 0)),
        ({"format_version": "1.9"}, "state", (1, 9)),
        ({"format_version": "1.2"}, "provider_schema", (1, 2)),
    ],
)
def test_validate_document_header_accepts_supported_major_and_preserves_minor_fields(
    value, kind, expected
):
    header, document = validate_document_header(value, kind=kind)

    assert header.format_version == expected
    assert document is value


@pytest.mark.parametrize(
    ("value", "kind"),
    [
        ([], "plan"),
        ({}, "plan"),
        ({"format_version": 1, "errored": False}, "plan"),
        ({"format_version": "2.0", "errored": False}, "plan"),
        ({"format_version": "1.0"}, "plan"),
        ({"format_version": "1.0", "errored": "false"}, "plan"),
        ({"format_version": "1.0", "errored": True}, "plan"),
    ],
)
def test_validate_document_header_rejects_malformed_or_errored_documents(value, kind):
    with pytest.raises(ExternalDocumentError):
        validate_document_header(value, kind=kind)
