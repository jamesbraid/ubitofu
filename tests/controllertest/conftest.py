# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 James Braid
from pathlib import Path

import pytest

from .adopt import adopt_fleet
from .herder import run_fleet
from .sandbox import Sandbox
from .support import SEEDED, SIM, UOS, UOS_RUN_KWARGS, UOS_SEEDED, boot_flavor

# The fleet the herder is asked for. Only `model` is given: everything else
# — MAC, serial, name, address — is the herder's to allocate and report back,
# and a supplied MAC would have to be locally administered anyway. No
# gateway: the sim controller ships its own demo fleet and a second UGW
# adopt fails api.err.NoSecondGateway.
SIM_FLEET_REQUEST: dict = {
    "version": 1,
    "devices": [{"model": "USM8P"}, {"model": "U7PRO"}],
}


@pytest.fixture(scope="session")
def seeded_controller():
    yield from boot_flavor(SEEDED)


@pytest.fixture(scope="session")
def sim_controller():
    yield from boot_flavor(SIM)


@pytest.fixture(scope="session")
def uos_controller():
    yield from boot_flavor(UOS, run_kwargs=UOS_RUN_KWARGS)


@pytest.fixture(scope="session")
def uos_seeded_controller():
    """Owner-seeded UOS on 443 native, carrying a baked X-API-KEY
    (RunningController.api_key)."""
    yield from boot_flavor(UOS_SEEDED, run_kwargs=UOS_RUN_KWARGS)


@pytest.fixture(scope="session")
def sim_fleet(sim_controller):
    """A herded device fleet on the sim controller's network.

    Yields (controller, devices) once the herder reports `ready` — started,
    healthy and addressed, but NOT yet adopted. Adoption is the downstream
    driver's job (adopt.py): the herder holds no credentials and does no
    controller polling, and keeping that split visible here is the point.

    Ordered after the controller fixture, so teardown runs the other way
    round: the herder removes every device container before the controller
    and its network go away.
    """
    yield from run_fleet(sim_controller, SIM_FLEET_REQUEST)


@pytest.fixture(scope="session")
def adopted_sim_fleet(sim_fleet):
    """The same fleet, driven to connected once for the whole session.

    For scenarios where adoption is setup rather than the thing under test.
    Adoption costs tens of seconds per device, so it happens here instead of
    per test; it is idempotent, returning as soon as every MAC reads back
    connected.
    """
    controller, devices = sim_fleet
    adopt_fleet(controller, [d.mac for d in devices])
    return controller, devices


@pytest.fixture(scope="session")
def plugin_cache(tmp_path_factory) -> Path:
    cache = tmp_path_factory.mktemp("tf-plugin-cache")
    return cache


@pytest.fixture
def make_sandbox(tmp_path, plugin_cache, monkeypatch):
    def factory(controller, site: str) -> Sandbox:
        return Sandbox(tmp_path / f"wd-{site}", controller, site, plugin_cache, monkeypatch)

    return factory
