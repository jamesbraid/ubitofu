# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 James Braid
import pytest

from .seeder import Seeder

pytestmark = pytest.mark.controller


def test_sandbox_init_and_ubitofu_generate(seeded_controller, make_sandbox, capsys):
    s = Seeder(seeded_controller)
    site = s.add_site("sandbox-generate")
    s.create_network(site, "sbx-net", vlan=202, subnet="10.99.202.1/24")
    sbx = make_sandbox(seeded_controller, site)
    sbx.init()
    code = sbx.ubitofu("generate")
    out = capsys.readouterr().out
    assert code == 0, out
    generated = (sbx.workdir / "generated.tf").read_text()
    assert 'resource "unifi_network"' in generated
    assert "sbx-net" in generated or "sbx_net" in generated  # slug or name literal
    assert (sbx.workdir / "imports.tf").exists()
    s.close()
