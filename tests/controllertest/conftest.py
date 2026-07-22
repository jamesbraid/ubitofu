# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 James Braid
from pathlib import Path

import pytest

from .sandbox import Sandbox
from .support import SEEDED, SIM, UOS, UOS_RUN_KWARGS, boot_flavor


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
def plugin_cache(tmp_path_factory) -> Path:
    cache = tmp_path_factory.mktemp("tf-plugin-cache")
    return cache


@pytest.fixture
def make_sandbox(tmp_path, plugin_cache, monkeypatch):
    def factory(controller, site: str) -> Sandbox:
        return Sandbox(tmp_path / f"wd-{site}", controller, site, plugin_cache, monkeypatch)

    return factory
