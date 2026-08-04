# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 James Braid
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
    # The fleet used to populate a few seconds after login readiness (observed
    # 0 devices for ~6s post-boot, then 9), so this polled for it. Readiness
    # now covers the fleet, which turns the poll into the assertion: this
    # checks the image's claim rather than working around its absence.
    devices = s.list_devices(sim_controller.site)
    # >= 8: the deleted-device scenario (test_scenarios_devices) removes one
    # demo AP from the shared default site
    assert len(devices) >= 8, "sim contract seeds 3 APs + 1 gateway + 5 switches"
    assert all(d.get("mac") for d in devices)
    s.close()
