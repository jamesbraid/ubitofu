# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 James Braid
import re

from . import pins


def test_versions_are_semver():
    assert re.fullmatch(r"\d+\.\d+\.\d+", pins.NETWORK_VERSION)
    assert re.fullmatch(r"\d+\.\d+\.\d+", pins.UOS_VERSION)


def test_images_derive_from_pins():
    assert pins.SEEDED_IMAGE == f"ghcr.io/jamesbraid/unifi-network:{pins.NETWORK_VERSION}-seeded"
    assert pins.SIM_IMAGE == f"ghcr.io/jamesbraid/unifi-network:{pins.NETWORK_VERSION}-sim"
    assert pins.UOS_IMAGE == f"ghcr.io/jamesbraid/unifi-os-server:{pins.UOS_VERSION}-sim"
