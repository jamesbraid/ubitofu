# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 James Braid
"""Live reconcile scenarios (S1..) — spec table in
docs/superpowers/specs/2026-07-19-container-controller-testing-design.md."""
import json

import pytest

from .scenario import adopt
from .seeder import Seeder

pytestmark = pytest.mark.controller


@pytest.fixture
def seeder(seeded_controller):
    s = Seeder(seeded_controller)
    yield s
    s.close()


@pytest.mark.skip(
    reason="parked on two jamesbraid/unifi 0.101.1 import bugs. Adopting the "
    "site still fails with api.err.DisablingDefaultNetworkNotAllowed, and the "
    "ordinary network still trips the domain_name null-to-empty-string "
    "consistency check. See docs/provider-import-bugs.md."
)
def test_s1_in_sync_reconcile_exits_zero(seeded_controller, seeder, make_sandbox, capsys):
    site = seeder.add_site("s1-in-sync")
    seeder.create_network(site, "s1-net", vlan=210, subnet="10.99.210.1/24")
    sbx = adopt(seeded_controller, site, make_sandbox)
    capsys.readouterr()  # drop adoption output
    code = sbx.ubitofu("reconcile")
    out = capsys.readouterr().out
    assert code == 0, out
    outcome = json.loads(out)["outcome"]
    assert outcome["blocked"] is False
    assert {item["reason_code"] for item in outcome["items"]} <= {"no_change"}
