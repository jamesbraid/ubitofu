# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 James Braid
"""Forgotten devices remain safe when the sandbox has no prior state."""
import json
import time

import pytest

from .seeder import Seeder

pytestmark = pytest.mark.controller


def test_s6b_unadopted_device_classified_forbidden_not_pending(
        sim_controller, make_sandbox, capsys):
    s = Seeder(sim_controller)
    # Pytest collection runs this module before test_seeder.py, so nothing
    # has yet waited out the sim's boot-completion race documented in
    # test_seeder.test_sim_has_demo_devices (fleet populates a few seconds
    # after login readiness). Poll here too — never skip on empty.
    deadline = time.monotonic() + 30.0
    devices: list[dict] = []
    while time.monotonic() < deadline:
        devices = s.list_devices(sim_controller.site)
        if devices:
            break
        time.sleep(2.0)
    assert devices, "sim contract seeds devices"
    # The sim assigns each demo device a random model at boot; some AP
    # models come up "unsupported" (unrecognized by this controller
    # version) and genuinely cannot be adopted (api.err.CannotAdopt — a
    # real rejection, not a bug). delete_device() adopts before deleting
    # (see seeder.py), so the victim must be adoptable. Prefer an AP (S6b's
    # canonical case) but fall back to any adoptable device: empirically
    # every demo AP can land "unsupported" in the same boot (observed), and
    # the mac_or_id identity/classify_diverged code path under test doesn't
    # care about device type.
    adoptable = [d for d in devices if not d.get("unsupported")]
    assert adoptable, "sim contract seeds at least one adoptable device"
    aps = [d for d in adoptable if d.get("type") == "uap"]
    victim_mac = (aps[0] if aps else adoptable[0])["mac"]

    sbx = make_sandbox(sim_controller, sim_controller.site)
    (sbx.workdir / "device.tf").write_text(
        f'resource "unifi_device" "demo_ap" {{\n  mac = "{victim_mac}"\n}}\n'
    )
    sbx.init()

    s.delete_device(sim_controller.site, victim_mac)

    # v2 gate, immediately before the only step that needs v2. The sim's v2
    # surface lags the v1 one — v2/firewall-policies 500s while ZBF defaults
    # materialize — and ubitofu's enumerate fails loud on it (correct product
    # behavior). Gating at the top of the test does not hold: adopting a device
    # (delete_device adopts first) makes the controller recompute zone-firewall
    # state and v2 500s again, so a gate placed before the mutations passes and
    # the reconcile after them still dies. Gate the operation, not the test.
    #
    # Ready means 200, not merely "not 500": a 401/403 is a broken session, not
    # a warming controller, and breaking on it would march into a confusing
    # failure downstream instead of naming the real status here. Two consecutive
    # reads, so a lone transient cannot latch it open.
    #
    # Deletable once a -sim image ships unifi-containers' v2-aware healthcheck.
    deadline = time.monotonic() + 90.0
    consecutive = 0
    while consecutive < 2:
        status = s.v2_status(sim_controller.site)
        consecutive = consecutive + 1 if status == 200 else 0
        if consecutive >= 2:
            break
        assert time.monotonic() < deadline, (
            f"sim v2 firewall-policies still HTTP {status} after 90s"
        )
        time.sleep(2.0)

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
