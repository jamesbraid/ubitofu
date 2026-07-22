# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 James Braid
"""S0: per-flavor smoke — readiness plus version enforcement. A wrong,
stale, or mistagged image must fail, not warn (contract: Version
enforcement)."""
import os

import pytest

from . import pins
from .readiness import login_client

pytestmark = pytest.mark.controller


def _live_network_version(ctl) -> str:
    with login_client(ctl.base_url, ctl.username, ctl.password) as client:
        body = client.get(f"/api/s/{ctl.site}/stat/sysinfo").json()
    return str(body["data"][0]["version"])


def test_seeded_smoke_version(seeded_controller):
    live = _live_network_version(seeded_controller)
    expected = os.environ.get("UNIFI_TEST_EXPECT_VERSION")
    if expected is None and not seeded_controller.external:
        expected = pins.NETWORK_VERSION  # container mode: image derives from the pin
    if expected is not None:
        assert live == expected, (
            f"live controller reports {live}, expected {expected} — "
            "stale or mistagged image, or pin drift"
        )
