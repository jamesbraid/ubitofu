# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 James Braid
"""S6b: the deleted-DEVICE advice, live. Committed unifi_device block with a
MAC identity, no state, object removed on the controller → classify_diverged
must say deleted via the committed-values identity branch (devices carry
their MAC in config; apply cannot recreate an adopted device)."""
import time

import pytest

from ubitofu.pipeline import EXIT_ATTENTION, EXIT_DRIFT_AND_ATTENTION

from .seeder import Seeder

pytestmark = pytest.mark.controller


def test_s6b_deleted_device_classified_deleted_not_pending(
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
    capsys.readouterr()
    code = sbx.ubitofu("reconcile")
    out = capsys.readouterr().out
    s.close()
    assert code in (EXIT_ATTENTION, EXIT_DRIFT_AND_ATTENTION), out
    assert "deleted" in out and "demo_ap" in out, out
    assert "pending" not in out.split("demo_ap")[-1].splitlines()[0], \
        "device must classify deleted (committed-values MAC identity), not pending"
