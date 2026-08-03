# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 James Braid
import re
from pathlib import Path

from . import pins


def test_versions_are_semver():
    assert re.fullmatch(r"\d+\.\d+\.\d+", pins.NETWORK_VERSION)
    assert re.fullmatch(r"\d+\.\d+\.\d+", pins.UOS_VERSION)
    assert re.fullmatch(r"\d+\.\d+\.\d+", pins.EMU_VERSION)
    assert re.fullmatch(r"\d+\.\d+\.\d+", pins.PROVIDER_VERSION)


def test_provider_source_names_its_registry():
    # A bare "namespace/name" resolves against the OpenTofu registry, which has
    # no jamesbraid namespace — `tofu init` would 404. The host is not optional.
    host, _, rest = pins.PROVIDER_SOURCE.partition("/")
    assert "." in host, f"{pins.PROVIDER_SOURCE!r} has no registry host"
    assert rest.count("/") == 1, f"{pins.PROVIDER_SOURCE!r} is not host/namespace/name"


def test_builds_belong_to_their_version():
    # A build is `<upstream>-<n>`, so a build string that has drifted off its
    # version pin would name an image nothing else here refers to.
    assert re.fullmatch(rf"{re.escape(pins.NETWORK_VERSION)}-\d+", pins.NETWORK_BUILD)
    assert re.fullmatch(rf"{re.escape(pins.UOS_VERSION)}-\d+", pins.UOS_BUILD)


def test_digests_are_well_formed():
    for name, digest in (
        ("SEEDED_DIGEST", pins.SEEDED_DIGEST),
        ("SIM_DIGEST", pins.SIM_DIGEST),
        ("UOS_DIGEST", pins.UOS_DIGEST),
        ("UOS_SEEDED_DIGEST", pins.UOS_SEEDED_DIGEST),
    ):
        assert re.fullmatch(r"sha256:[0-9a-f]{64}", digest), f"{name}={digest!r}"


def test_images_are_digest_pinned():
    # The tag alone cannot identify a build — upstream rebuilds a published
    # version in place and build numbers never reach the tag — and
    # testcontainers will not re-pull a tag it already holds. An image
    # reference that lost its digest would silently run whatever is cached.
    for name, image, digest in (
        ("SEEDED_IMAGE", pins.SEEDED_IMAGE, pins.SEEDED_DIGEST),
        ("SIM_IMAGE", pins.SIM_IMAGE, pins.SIM_DIGEST),
        ("UOS_IMAGE", pins.UOS_IMAGE, pins.UOS_DIGEST),
        ("UOS_SEEDED_IMAGE", pins.UOS_SEEDED_IMAGE, pins.UOS_SEEDED_DIGEST),
    ):
        assert image.endswith(f"@{digest}"), f"{name} is not digest-pinned: {image}"


def test_no_image_uses_a_sliding_tag():
    # `latest` / `sim` / `seeded` follow the highest stable upstream version,
    # which is no longer the one pinned here — using one would jump the
    # controller version behind what looks like a variant selector.
    for image in (pins.SEEDED_IMAGE, pins.SIM_IMAGE, pins.UOS_IMAGE, pins.UOS_SEEDED_IMAGE):
        tag = image.split("@", 1)[0].rsplit(":", 1)[1]
        assert tag not in ("latest", "sim", "seeded"), image


def test_images_derive_from_pins():
    net = "ghcr.io/jamesbraid/unifi-network"
    uos = "ghcr.io/jamesbraid/unifi-os-server"
    assert pins.SEEDED_IMAGE == (
        f"{net}:{pins.NETWORK_VERSION}-seeded@{pins.SEEDED_DIGEST}"
    )
    assert pins.SIM_IMAGE == f"{net}:{pins.NETWORK_VERSION}-sim@{pins.SIM_DIGEST}"
    assert pins.UOS_IMAGE == f"{uos}:{pins.UOS_VERSION}-sim@{pins.UOS_DIGEST}"
    assert pins.EMU_SYNTHETIC_IMAGE == f"ghcr.io/jamesbraid/unifi-emu:{pins.EMU_VERSION}"
    assert pins.UOS_SEEDED_IMAGE == (
        f"{uos}:{pins.UOS_VERSION}-seeded@{pins.UOS_SEEDED_DIGEST}"
    )


# ---------------------------------------------------------------------------
# Pin-drift tripwire: the CI configs (read as plain text, no yaml dependency)
# must never fall out of sync with pins.py or with each other. A bumped
# pins.py that forgets to bump .woodpecker/controller.yml or
# .github/workflows/controller-tests.yml is exactly the drift this catches —
# CI would otherwise silently keep testing a stale image tag.
# ---------------------------------------------------------------------------

# The regexes below scan the CI files as raw text, comments included — a
# version literal left in a YAML comment trips these checks exactly like a
# live one, and that's deliberate (fixing the drift is cheaper than special-
# casing comments out of the scan).
_REPO_ROOT = Path(__file__).resolve().parents[2]
_WOODPECKER = _REPO_ROOT / ".woodpecker" / "controller.yml"
_GHA = _REPO_ROOT / ".github" / "workflows" / "controller-tests.yml"

_NETWORK_TAG_RE = re.compile(r"ghcr\.io/jamesbraid/unifi-network:(\S+)")
_UOS_TAG_RE = re.compile(r"ghcr\.io/jamesbraid/unifi-os-server:(\S+)")
_HERDER_ACTION_RE = re.compile(
    r"jamesbraid/unifi-emu/\.github/actions/install-herder@v(\S+)"
)
_READY_URL_RE = re.compile(r"UNIFI_TEST_\w+_READY:\s*(\S+)")
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


def test_ci_image_references_carry_the_pinned_digest():
    # The version literal in a CI file no longer identifies a build: upstream
    # rebuilds a published version in place, and the runner would take
    # whatever that tag resolves to on the day. Every reference must name the
    # same digest pins.py does, or CI and the local suite silently diverge.
    by_variant = {
        "seeded": pins.SEEDED_DIGEST,
        "sim": pins.SIM_DIGEST,
    }
    seen = 0
    for path in (_WOODPECKER, _GHA):
        for ref in _NETWORK_TAG_RE.findall(path.read_text()):
            tag, _, digest = ref.partition("@")
            variant = tag.rsplit("-", 1)[-1]
            expected = by_variant.get(variant)
            assert expected, f"{path}: unifi-network tag {tag!r} names no known variant"
            assert digest == expected, (
                f"{path}: {tag} is pinned to {digest or '(no digest)'}, "
                f"but pins.py says {expected}"
            )
            seen += 1
    assert seen, "expected at least one digest-pinned unifi-network reference in CI"


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


def test_herder_action_in_gha_is_pinned_to_pins_emu_version():
    # The GHA workflow installs the herder through unifi-emu's own action,
    # whose ref selects the release. That binary carries a version-matched
    # synthetic image, so a ref naming a different release than
    # pins.EMU_VERSION would quietly test a different emulator than the one
    # this suite declares.
    refs = _HERDER_ACTION_RE.findall(_GHA.read_text())
    assert refs, f"{_GHA}: expected an install-herder action reference"
    for ref in refs:
        assert ref == pins.EMU_VERSION, (
            f"{_GHA}: install-herder@v{ref} != "
            f"pins.EMU_VERSION {pins.EMU_VERSION!r}"
        )


def test_woodpecker_names_no_herder():
    # Woodpecker's rootless agent has no docker socket, so it runs the suite
    # filtered "not herder" and must never grow a herder reference to drift.
    assert not _HERDER_ACTION_RE.search(_WOODPECKER.read_text()), (
        f"{_WOODPECKER}: unexpected install-herder reference "
        "(woodpecker never runs herder scenarios)"
    )


def test_ready_urls_use_the_pinned_readyz_endpoint():
    # Woodpecker cannot read the image's health verdict, so these URLs are the
    # only thing standing between it and a login-only wait. A port or path that
    # drifted from what the images serve would not fail loudly — readiness
    # would quietly fall back and race the v2 surface again.
    urls = _READY_URL_RE.findall(_WOODPECKER.read_text())
    assert urls, f"{_WOODPECKER}: expected at least one UNIFI_TEST_<FLAVOR>_READY"
    for url in urls:
        assert url.endswith(f":{pins.READYZ_PORT}{pins.READYZ_PATH}"), (
            f"{_WOODPECKER}: {url} does not end with "
            f":{pins.READYZ_PORT}{pins.READYZ_PATH}"
        )


def test_every_woodpecker_service_has_a_ready_url():
    # A service without one is the silent failure mode: the suite still runs,
    # still passes most days, and waits on a login that goes green before the
    # v2 surface and the demo fleet do.
    text = _WOODPECKER.read_text()
    # Only the services block — `steps:` uses the same `- name:` shape, and a
    # step is not something readiness applies to.
    block = text.split("\nservices:", 1)[1].split("\nsteps:", 1)[0]
    services = set(re.findall(r"^\s+- name: (\S+)$", block, re.MULTILINE))
    assert services, f"{_WOODPECKER}: found no services to check"
    ready = {m.lower() for m in re.findall(r"UNIFI_TEST_(\w+)_READY:", text)}
    missing = {s for s in services if s.replace("-", "_") not in ready}
    assert not missing, f"{_WOODPECKER}: services with no _READY url: {sorted(missing)}"


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
    # UNIFI_TEST_UOS_EXPECT_VERSION has no pin of its own — it is the UOS
    # bundle's own network-app version. The UniFi OS smoke documents that it
    # matches pins.NETWORK_VERSION today by coincidence, not by contract; see
    # test_uos_smoke_version in test_scenarios_uos.py.
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
