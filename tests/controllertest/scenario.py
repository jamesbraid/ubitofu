# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 James Braid
"""Shared write-scenario mechanics for coverage-complete controller sites."""
from .sandbox import Sandbox


def adopt(controller, site: str, make_sandbox) -> Sandbox:
    """Generate and apply only after the site has full supported coverage."""
    sbx = make_sandbox(controller, site)
    sbx.init()
    code = sbx.ubitofu("generate")
    assert code == 0, (
        "adoption requires coverage-complete generation. Inspect the receipt "
        "for coverage gaps before testing provider import/apply"
    )
    sbx.apply()
    return sbx
