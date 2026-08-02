# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 James Braid
"""Live-controller coverage for the power-supervisor enumeration skip.

The unit tests hand-build the v2 wire shape from the go-unifi struct. These
prove it against a real controller: that the endpoint exists and is reachable
through Controller.collection, and that enumeration of a site carrying a real
power-supervisor record neither crashes nor emits an import target.
"""
import time

import pytest

from ubitofu.controller import Controller
from ubitofu.enumerator import enumerate_controller

from .seeder import Seeder, SeedError

pytestmark = pytest.mark.controller

_ENDPOINT = "v2/api/site/{site}/power-supervisors"

# The controller's rejection when a candidate device has no PoE uplink.
_POE_UPLINK_ERR = "api.err.PurePoeRequiresUplinkException"


@pytest.fixture
def seeder(seeded_controller):
    s = Seeder(seeded_controller)
    yield s
    s.close()


@pytest.fixture
def sim_seeder(sim_controller):
    s = Seeder(sim_controller)
    yield s
    s.close()


def _classic(ctl, site):
    return Controller(base_url=ctl.base_url, site=site, dialect="classic",
                      username=ctl.username, password=ctl.password)


def test_power_supervisor_endpoint_is_reachable(seeded_controller, seeder):
    """The endpoint must answer, not 404.

    Controller.collection calls raise_for_status, so a controller that does not
    serve this path turns every reconcile into an HTTP crash. The manifest maps
    the endpoint unconditionally, so this is load-bearing for all sites, not
    just ones that have a supervisor.
    """
    site = seeder.add_site("psup-reach")
    ctl = _classic(seeded_controller, site)
    try:
        records = ctl.collection(_ENDPOINT)
    finally:
        ctl.close()
    assert isinstance(records, list)  # empty is fine; a 404 would have raised


def test_seeded_power_supervisor_is_skipped_not_crashed(sim_controller, sim_seeder):
    """A site carrying a real supervisor record enumerates without crashing.

    Regression for the reconcile abort seen in pipelines 934/982: identity
    derivation used to raise ValueError on this record shape. Skipped rather
    than adopted, so it must produce a gap and no import target.

    Runs on sim, not seeded: a supervisor references a power-consumer device,
    and only the sim image seeds adoptable devices. The device must be adopted
    first or the controller 404s api.err.PowerConsumerDeviceNotFound.
    """
    seeder = sim_seeder
    site = sim_controller.site

    # The sim's fleet populates a few seconds after login readiness, and its
    # v2 surface 500s for a window while ZBF defaults materialize. Both are
    # documented boot races (see test_scenarios_devices.py) — poll, never skip
    # on empty, or the gate is vacuously green.
    deadline = time.monotonic() + 30.0
    devices: list[dict] = []
    while time.monotonic() < deadline:
        devices = seeder.list_devices(site)
        if devices:
            break
        time.sleep(2.0)
    assert devices, "sim contract seeds devices"

    deadline = time.monotonic() + 60.0
    while True:
        status = seeder.v2_status(site)
        if status < 500:
            break
        assert time.monotonic() < deadline, f"sim v2 still HTTP {status} after 60s"
        time.sleep(2.0)

    # Some demo APs come up "unsupported" at random each boot and genuinely
    # cannot be adopted (api.err.CannotAdopt).
    adoptable = [d for d in devices if not d.get("unsupported") and d.get("mac")]
    assert adoptable, "sim contract seeds at least one adoptable device"

    created: dict = {}
    errors: list[str] = []
    for dev in adoptable:
        mac = str(dev["mac"])
        try:
            if not dev.get("adopted"):
                seeder.adopt_device(site, mac)
            created = seeder.create_power_supervisor(site, mac)
            break
        except SeedError as exc:
            errors.append(f"{mac} ({dev.get('model', '?')}): {exc}")
    if not created:
        # Skip ONLY on the documented capability limit, never on a generic
        # failure — a gate that skips on any error is vacuously green.
        # A supervisor must reference a device actually powered by a PoE port
        # on another adopted device. The sim's demo fleet has no PoE uplink
        # relationships, so every device rejects with UPLINK_NOT_FOUND
        # (verified across all 9 seeded devices, controller 10.4.57).
        # Un-park by pointing UNIFI_TEST_SIM_URL at a controller with real PoE
        # topology; the scenario then runs for real with no code change.
        unrelated = [e for e in errors if _POE_UPLINK_ERR not in e]
        assert not unrelated, (
            "power-supervisor seeding failed for reasons other than the known "
            "sim PoE-topology limit:\n  " + "\n  ".join(unrelated)
        )
        pytest.skip(
            f"sim fleet has no PoE uplink topology ({_POE_UPLINK_ERR}); "
            f"{len(errors)} device(s) rejected"
        )

    # Prove the wire shape the unit tests assume, against the real controller.
    assert "client_mac" in created, f"expected client_mac in {created!r}"
    assert "mac" not in created, f"unexpected mac key in {created!r}"
    assert created.get("id"), f"expected a controller id in {created!r}"
    assert "_id" not in created, f"unexpected _id key in {created!r}"

    ctl = _classic(sim_controller, site)
    try:
        res = enumerate_controller(ctl)  # full manifest, as reconcile runs it
    finally:
        ctl.close()

    assert not [t for t in res.targets if t.resource_type == "unifi_power_supervisor"]
    assert any("power supervisor(s)" in g for g in res.gaps), res.gaps
