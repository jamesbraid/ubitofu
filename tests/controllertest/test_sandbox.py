# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 James Braid
import json

import pytest

from .seeder import Seeder

pytestmark = pytest.mark.controller


def test_sandbox_generate_blocks_live_coverage_gaps_without_candidates(
    seeded_controller, make_sandbox, capsys
):
    """Real coverage gaps suppress every candidate from a non-empty site."""
    s = Seeder(seeded_controller)
    site = s.add_site("sandbox-generate")
    s.create_network(site, "sbx-net", vlan=202, subnet="10.99.202.1/24")
    sbx = make_sandbox(seeded_controller, site)
    sbx.init()
    code = sbx.ubitofu("generate")
    out = capsys.readouterr().out
    receipt = json.loads(out)
    items = receipt["outcome"]["items"]
    reasons = {item["reason_code"] for item in items}

    assert code == 3, out
    assert receipt["outcome"]["blocked"] is True
    assert "coverage_gap" in reasons
    assert "generation_blocked" in reasons
    assert receipt["outcome"]["payload"] == {
        "candidate_digests": [],
        "changed_paths": [],
    }
    assert not (sbx.workdir / "generated.tf").exists()
    assert not (sbx.workdir / "imports.tf").exists()
    assert not (sbx.workdir / "COVERAGE.md").exists()
    s.close()
