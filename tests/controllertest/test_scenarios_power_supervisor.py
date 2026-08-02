# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 James Braid
"""Live-controller coverage for the power-supervisor enumeration skip.

The unit tests hand-build the v2 wire shape from the go-unifi struct. These
prove it against a controller: that the endpoint answers rather than 404s, and
that a site holding a real supervisor record enumerates to a coverage gap
instead of the ValueError that used to abort reconcile.

Both scenarios take one controller, whichever UNIFI_TEST_SEEDED_URL names, so
pointing that at real hardware runs the whole file against it. The container
images cannot substitute for the second scenario: a power supervisor
references a device drawing power from a PoE port on another adopted device,
and neither image models an uplink. The sim's demo fleet rejects every
candidate with api.err.PurePoeRequiresUplinkException (verified across the
whole fleet on 10.4.57), and the seeded image ships no devices at all. Real
PoE topology is the only thing that satisfies it.
"""
import os
import time

import pytest

from ubitofu.controller import Controller
from ubitofu.enumerator import enumerate_controller

from .seeder import Seeder, SeedError

pytestmark = pytest.mark.controller

_ENDPOINT = "v2/api/site/{site}/power-supervisors"

#: Set UNIFI_TEST_SEEDED_URL to a controller with real PoE topology to run the
#: seeding scenario. Checked as a skipif so pytest evaluates it before fixtures
#: and no container boots for a scenario that cannot pass on one.
_REAL_CONTROLLER = bool(os.environ.get("UNIFI_TEST_SEEDED_URL"))
_NEEDS_REAL = "needs a controller with real PoE topology; set UNIFI_TEST_SEEDED_URL"


@pytest.fixture
def seeder(seeded_controller):
    s = Seeder(seeded_controller)
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
    site = seeded_controller.site if _REAL_CONTROLLER else seeder.add_site("psup-reach")
    ctl = _classic(seeded_controller, site)
    try:
        records = ctl.collection(_ENDPOINT)
    finally:
        ctl.close()
    assert isinstance(records, list)  # empty is fine; a 404 would have raised


@pytest.mark.skipif(not _REAL_CONTROLLER, reason=_NEEDS_REAL)
def test_live_power_supervisor_is_skipped_not_crashed(seeded_controller, seeder):
    """A site holding a real supervisor enumerates to a gap, never a crash.

    Regression for the reconcile abort in pipelines 934/982: identity derivation
    raised ValueError on this record shape because the manifest keyed it by mac
    and the v2 record carries client_mac. Adoption is parked, so the objects must
    be counted as a gap and emit no import target.

    Asserts the wire shape against the live record rather than the go-unifi
    struct, which is the part the unit tests can only assume.
    """
    site = seeded_controller.site
    records = []
    deadline = time.monotonic() + 30.0
    while time.monotonic() < deadline:
        ctl = _classic(seeded_controller, site)
        try:
            records = ctl.collection(_ENDPOINT)
        finally:
            ctl.close()
        if records:
            break
        time.sleep(2.0)
    if not records:
        pytest.skip(f"controller has no power supervisor on site {site!r}")

    for rec in records:
        assert "client_mac" in rec, f"expected client_mac in {rec!r}"
        assert "mac" not in rec, f"unexpected mac key in {rec!r}"
        assert rec.get("id"), f"expected a controller id in {rec!r}"
        assert "_id" not in rec, f"unexpected _id key in {rec!r}"

    ctl = _classic(seeded_controller, site)
    try:
        res = enumerate_controller(ctl)  # full manifest, as reconcile runs it
    finally:
        ctl.close()

    assert not [t for t in res.targets if t.resource_type == "unifi_power_supervisor"]
    assert f"{len(records)} device power supervisor(s)" in " ".join(res.gaps), res.gaps


def test_seeder_can_report_a_power_supervisor_rejection(seeded_controller, seeder):
    """Creating a supervisor fails loudly, with a reason, not silently.

    Keeps Seeder.create_power_supervisor honest: it must raise SeedError
    carrying the controller's own message. Every container-image device rejects
    (no PoE uplink), which is exactly the path being pinned here.
    """
    if _REAL_CONTROLLER:
        pytest.skip("would mutate a real controller")
    site = seeder.add_site("psup-reject")
    with pytest.raises(SeedError) as exc:
        seeder.create_power_supervisor(site, "58:d6:1f:00:00:0a")
    assert "power-supervisors" in str(exc.value)
