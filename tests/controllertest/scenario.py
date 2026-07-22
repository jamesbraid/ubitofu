# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 James Braid
"""Shared write-scenario mechanics: adopt whatever the site holds."""
from .sandbox import Sandbox


def adopt(controller, site: str, make_sandbox) -> Sandbox:
    """generate → init → apply: brings the site under tofu management."""
    sbx = make_sandbox(controller, site)
    sbx.init()
    code = sbx.ubitofu("generate")
    assert code == 0, "generate must succeed before adoption"
    sbx.apply()
    return sbx
