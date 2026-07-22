# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 James Braid
import time

import pytest

from .seeder import Seeder, SeedError

pytestmark = pytest.mark.controller


def test_seed_network_roundtrip(seeded_controller):
    s = Seeder(seeded_controller)
    site = s.add_site("seeder-roundtrip")
    created = s.create_network(site, "seed-net", vlan=201, subnet="10.99.201.1/24")
    assert created["_id"]
    s.update_network(site, created["_id"], {"name": "seed-net-renamed"})
    names = {n["name"] for n in s.list_networks(site)}
    assert "seed-net-renamed" in names
    s.delete_network(site, created["_id"])
    s.close()


def test_seed_failure_raises_not_skips(seeded_controller):
    s = Seeder(seeded_controller)
    with pytest.raises(SeedError):
        s.create_network("nonexistent-site", "x", vlan=1, subnet="not-a-subnet")
    s.close()


def test_sim_has_demo_devices(sim_controller):
    s = Seeder(sim_controller)
    # Empirical (unifi-network 10.4.57-sim): the simulated device fleet
    # populates a few seconds *after* login readiness succeeds (observed
    # 0 devices for ~6s post-boot, then 9). That's a boot-completion race,
    # not a seed failure — poll for it, but never skip on empty: if the
    # fleet never appears within the deadline the final assert still fails.
    deadline = time.monotonic() + 30.0
    devices: list[dict] = []
    while time.monotonic() < deadline:
        devices = s.list_devices(sim_controller.site)
        if len(devices) >= 9:
            break
        time.sleep(2.0)
    assert len(devices) >= 9, "sim contract seeds 3 APs + 1 gateway + 5 switches"
    assert all(d.get("mac") for d in devices)
    s.close()
