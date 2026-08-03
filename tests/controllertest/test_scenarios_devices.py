# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 James Braid
"""A configured device absent from state and live is a forbidden create."""
import pytest

from ubitofu.pipeline import EXIT_FORBIDDEN_CREATE

from .seeder import Seeder

pytestmark = pytest.mark.controller


def test_untracked_deleted_device_is_forbidden_create(
        sim_controller, make_sandbox, capsys):
    s = Seeder(sim_controller)
    # No fleet poll: readiness now includes the full demo fleet, so a
    # controller this fixture hands over has all of it (unifi-containers
    # gates its healthcheck and /readyz on stat/device reaching the seeded
    # count, not on the first device to appear).
    devices = s.list_devices(sim_controller.site)
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

    capsys.readouterr()
    code = sbx.ubitofu("reconcile")
    captured = capsys.readouterr()
    out = captured.out
    s.close()
    assert code == EXIT_FORBIDDEN_CREATE, (
        f"exit={code}\nSTDOUT:\n{out}\nSTDERR:\n{captured.err}"
    )
    assert "Forbidden (device create" in out and "demo_ap" in out, out
