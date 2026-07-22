# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 James Braid
import pytest

from .support import SEEDED, SIM, boot_flavor


@pytest.fixture(scope="session")
def seeded_controller():
    yield from boot_flavor(SEEDED)


@pytest.fixture(scope="session")
def sim_controller():
    yield from boot_flavor(SIM)
