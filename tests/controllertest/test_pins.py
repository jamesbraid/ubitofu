# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 James Braid
import re
from pathlib import Path

from . import pins


def test_versions_are_semver():
    assert re.fullmatch(r"\d+\.\d+\.\d+", pins.NETWORK_VERSION)
    assert re.fullmatch(r"\d+\.\d+\.\d+", pins.UOS_VERSION)


def test_images_derive_from_pins():
    assert pins.SEEDED_IMAGE == f"ghcr.io/jamesbraid/unifi-network:{pins.NETWORK_VERSION}-seeded"
    assert pins.SIM_IMAGE == f"ghcr.io/jamesbraid/unifi-network:{pins.NETWORK_VERSION}-sim"
    assert pins.UOS_IMAGE == f"ghcr.io/jamesbraid/unifi-os-server:{pins.UOS_VERSION}-sim"


# ---------------------------------------------------------------------------
# Pin-drift tripwire: the CI configs (read as plain text, no yaml dependency)
# must never fall out of sync with pins.py or with each other. A bumped
# pins.py that forgets to bump .woodpecker/controller.yml or
# .github/workflows/controller-tests.yml is exactly the drift this catches —
# CI would otherwise silently keep testing a stale image tag.
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parents[2]
_WOODPECKER = _REPO_ROOT / ".woodpecker" / "controller.yml"
_GHA = _REPO_ROOT / ".github" / "workflows" / "controller-tests.yml"

_NETWORK_TAG_RE = re.compile(r"ghcr\.io/jamesbraid/unifi-network:(\S+)")
_UOS_TAG_RE = re.compile(r"ghcr\.io/jamesbraid/unifi-os-server:(\S+)")
_EXPECT_VERSION_RE = re.compile(r'UNIFI_TEST_EXPECT_VERSION:\s*"([^"]+)"')
_UOS_EXPECT_VERSION_RE = re.compile(r'UNIFI_TEST_UOS_EXPECT_VERSION:\s*"([^"]+)"')


def test_network_image_tags_start_with_pins_network_version():
    # woodpecker's `services:` pin the image by literal tag; the GHA
    # workflow boots via testcontainers straight off pins.py and carries
    # no literal image reference at all today — its loop below is a
    # forward guard (a future hardcoded override must stay pinned too),
    # not a claim that a tag currently exists there.
    woodpecker_text = _WOODPECKER.read_text()
    woodpecker_tags = _NETWORK_TAG_RE.findall(woodpecker_text)
    assert woodpecker_tags, f"{_WOODPECKER}: expected at least one unifi-network image tag"
    for path in (_WOODPECKER, _GHA):
        for tag in _NETWORK_TAG_RE.findall(path.read_text()):
            assert tag.startswith(pins.NETWORK_VERSION), (
                f"{path}: unifi-network tag {tag!r} does not start with "
                f"pins.NETWORK_VERSION {pins.NETWORK_VERSION!r}"
            )


def test_uos_image_tag_starts_with_pins_uos_version_in_gha():
    # UOS only ever runs in the GHA workflow (woodpecker services are
    # seeded/sim only — the suite there is filtered "controller and not
    # uos") — a unifi-os-server tag has no business appearing in the
    # woodpecker config at all. The GHA workflow itself boots UOS via
    # testcontainers off pins.py with no literal tag today, same as the
    # network image above — this loop is the forward guard.
    for tag in _UOS_TAG_RE.findall(_GHA.read_text()):
        assert tag.startswith(pins.UOS_VERSION), (
            f"{_GHA}: unifi-os-server tag {tag!r} does not start with "
            f"pins.UOS_VERSION {pins.UOS_VERSION!r}"
        )
    assert not _UOS_TAG_RE.search(_WOODPECKER.read_text()), (
        f"{_WOODPECKER}: unexpected unifi-os-server reference "
        "(woodpecker never runs uos scenarios)"
    )


def test_expect_version_equals_pins_network_version_everywhere():
    for path in (_WOODPECKER, _GHA):
        text = path.read_text()
        values = _EXPECT_VERSION_RE.findall(text)
        assert values, f"{path}: expected at least one UNIFI_TEST_EXPECT_VERSION"
        for value in values:
            assert value == pins.NETWORK_VERSION, (
                f"{path}: UNIFI_TEST_EXPECT_VERSION={value!r} != "
                f"pins.NETWORK_VERSION {pins.NETWORK_VERSION!r}"
            )


def test_uos_expect_version_agrees_with_network_expect_version_in_gha():
    # UNIFI_TEST_UOS_EXPECT_VERSION has no pin of its own — it's the UOS
    # bundle's own network-app version, which test_scenarios_uos.py's S0
    # documents as matching pins.NETWORK_VERSION today by coincidence, not
    # by contract (see that file's test_s0_uos_smoke_version docstring).
    # The honest, unbrittle check is that the two env vars declared in the
    # same file agree with each other; a deliberate divergence should
    # update this test, not slip past it silently.
    text = _GHA.read_text()
    network_values = set(_EXPECT_VERSION_RE.findall(text))
    uos_values = set(_UOS_EXPECT_VERSION_RE.findall(text))
    assert uos_values, f"{_GHA}: expected at least one UNIFI_TEST_UOS_EXPECT_VERSION"
    assert uos_values == network_values, (
        f"{_GHA}: UNIFI_TEST_UOS_EXPECT_VERSION {sorted(uos_values)} disagrees "
        f"with UNIFI_TEST_EXPECT_VERSION {sorted(network_values)}"
    )
