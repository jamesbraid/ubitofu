# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 James Braid
"""A configured device absent from state and live is a forbidden create."""
import time

import pytest

from ubitofu.pipeline import EXIT_FORBIDDEN_CREATE

from .seeder import Seeder

pytestmark = pytest.mark.controller


def test_untracked_deleted_device_is_forbidden_create(
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
    # The sim gives each demo device a random model at boot, so the fleet
    # differs run to run and the victim has to be chosen, not assumed.
    # delete_device() adopts before deleting (see seeder.py), so the victim
    # must be a device this controller will actually adopt. Three kinds are
    # not: an AP whose model this controller version does not recognise
    # ("unsupported" — a real api.err.CannotAdopt), an LTE backup, which
    # refuses with api.err.LteDeviceAdoptingUnregistered until it is
    # registered, and a gateway, which is not what this scenario is about.
    #
    # Take an access point, or a switch when every AP this boot came up
    # unsupported — observed, and the reason the fallback exists. The
    # mac_or_id identity and classify_diverged path under test does not care
    # which of the two it gets.
    adoptable = [d for d in devices
                 if not d.get("unsupported") and d.get("type") in ("uap", "usw")]
    assert adoptable, (
        "no adoptable access point or switch in the demo fleet: "
        + ", ".join(f"{d.get('type')}/{d.get('model')}"
                    f"{' unsupported' if d.get('unsupported') else ''}"
                    for d in devices))
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
    assert code == EXIT_FORBIDDEN_CREATE, (
        f"exit={code}\nSTDOUT:\n{out}\nSTDERR:\n{captured.err}"
    )
    assert "Forbidden (device create" in out and "demo_ap" in out, out
