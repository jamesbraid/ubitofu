# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 James Braid
"""Forgotten devices remain safe when the sandbox has no prior state."""
import json

import pytest

from .seeder import Seeder

pytestmark = pytest.mark.controller


def test_s6b_unadopted_device_classified_forbidden_not_pending(
        sim_controller, make_sandbox, capsys):
    s = Seeder(sim_controller)
    # The pinned image's healthcheck and /readyz contract include the demo
    # fleet, so a handed-over controller is already complete.
    devices = s.list_devices(sim_controller.site)
    assert devices, "sim contract seeds devices"
    # delete_device() adopts before deleting. LTE backups, gateways, and APs
    # unsupported by this controller are not valid victims, so prefer an AP
    # and fall back to a switch.
    adoptable = [
        d for d in devices
        if not d.get("unsupported") and d.get("type") in ("uap", "usw")
    ]
    assert adoptable, "sim contract seeds an adoptable access point or switch"
    aps = [d for d in adoptable if d.get("type") == "uap"]
    victim_mac = (aps[0] if aps else adoptable[0])["mac"]

    sbx = make_sandbox(sim_controller, sim_controller.site)
    (sbx.workdir / "device.tf").write_text(
        f'resource "unifi_device" "demo_ap" {{\n  mac = "{victim_mac}"\n}}\n'
    )
    sbx.init()

    s.delete_device(sim_controller.site, victim_mac)

    capsys.readouterr()
    code = sbx.ubitofu("reconcile")
    captured = capsys.readouterr()
    out = captured.out
    s.close()
    assert code == 3, (
        f"exit={code}\nSTDOUT:\n{out}\nSTDERR:\n{captured.err}"
    )
    outcome = json.loads(out)["outcome"]
    reasons = {item["reason_code"] for item in outcome["items"]}
    # Forget returns simulated hardware to unadopted discovery, which is not a
    # controller-managed object. This sandbox never applied the HCL, so ubitofu
    # has no prior state proving deletion rather than a newly authored device
    # block. The safe result is the normal UI-only create prohibition. Planner
    # tests cover state-backed deletion. Its live scenario remains parked on
    # provider import/apply support.
    assert "forbidden_device_create" in reasons
    assert "controller_resource_deleted" not in reasons
    assert "pending_create" not in reasons
