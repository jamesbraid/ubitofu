# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 James Braid
from pathlib import Path

import pytest

from .sandbox import Sandbox
from .support import SEEDED, SIM, boot_flavor


@pytest.fixture(scope="session")
def seeded_controller():
    yield from boot_flavor(SEEDED)


@pytest.fixture(scope="session")
def sim_controller():
    yield from boot_flavor(SIM)


@pytest.fixture(scope="session")
def plugin_cache(tmp_path_factory) -> Path:
    cache = tmp_path_factory.mktemp("tf-plugin-cache")
    return cache


@pytest.fixture
def make_sandbox(tmp_path, plugin_cache, monkeypatch):
    def factory(controller, site: str) -> Sandbox:
        return Sandbox(tmp_path / f"wd-{site}", controller, site, plugin_cache, monkeypatch)

    return factory
