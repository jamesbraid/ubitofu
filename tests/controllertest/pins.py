# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 James Braid
"""Single source of truth for controller test-target pins.

Contract: one version pin per flavor lineage; every image reference and
version expectation derives from these constants (env-overridable at the
fixture layer, never here).

Image references carry a digest as well as a tag, because the tag alone does
not identify a build. unifi-containers rebuilds a published version in place
when the image changes without an upstream change — 10.4.57-sim has been
rebuilt three times — and states outright that build numbers are a git
concept that never appears as an image tag. The rebuilds that matter here
taught the images to prove their own readiness, and nothing observable from
outside distinguishes them: the healthcheck's command string is identical
across builds, only the implementation inside changed. testcontainers will
not re-pull a tag it already holds, so a warm cache would silently run the
build whose readiness this suite no longer waits for. The digest is what
makes that impossible; the tag is kept for readability.

Never use the sliding `latest` / `sim` / `seeded` tags. They follow the
highest stable upstream version, which is now 10.5.67 — a controller-version
jump hiding behind a tag that looks like a variant selector.
"""

NETWORK_VERSION = "10.4.57"
UOS_VERSION = "5.1.21"
# The build behind each version pin, for the error message when a stale local
# image is what actually booted. 10.4.57 is a maintenance line now that
# upstream's default is 10.5.67, so it keeps receiving builds.
NETWORK_BUILD = "10.4.57-4"  # rev 5638d2a
UOS_BUILD = "5.1.21-6"  # rev a261a8c
# unifi-emu, which releases the herder binary and the synthetic device image
# from one tag. The binary carries its matching image reference compiled in,
# so pinning the version pins both halves and neither can float. No digest:
# a unifi-emu release tag is immutable, never rebuilt in place.
EMU_VERSION = "0.5.1"

SEEDED_DIGEST = "sha256:697d93108a13db254505a4154f2822ca0bf9131ae121bf023c1e92f1bf974a62"
SIM_DIGEST = "sha256:584be3a2e45c4913e1bc373eff9c7330609c82085d4fc6f5ea365abdcdb3e664"
UOS_DIGEST = "sha256:5d35a31f09611c4cc6bb9bf16c98056396aa02fe0f817efbe50da55bcde3ac0e"
UOS_SEEDED_DIGEST = "sha256:9661c539167a16d8c8f45a06586adeb37c0c615d8d854a3b4f2c07bb4623ec4b"

SEEDED_IMAGE = f"ghcr.io/jamesbraid/unifi-network:{NETWORK_VERSION}-seeded@{SEEDED_DIGEST}"
SIM_IMAGE = f"ghcr.io/jamesbraid/unifi-network:{NETWORK_VERSION}-sim@{SIM_DIGEST}"
UOS_IMAGE = f"ghcr.io/jamesbraid/unifi-os-server:{UOS_VERSION}-sim@{UOS_DIGEST}"
EMU_SYNTHETIC_IMAGE = f"ghcr.io/jamesbraid/unifi-emu:{EMU_VERSION}"
# The owner-seeded UOS variant: headless 443 login (unifi-core /api/setup),
# real empty site, no 7443 direct port. Its own flavor, distinct from the
# -sim UOS above — the native-dialect (443) test target.
UOS_SEEDED_IMAGE = (
    f"ghcr.io/jamesbraid/unifi-os-server:{UOS_VERSION}-seeded@{UOS_SEEDED_DIGEST}"
)

#: Port the images serve their readiness verdict on: GET /readyz, 200 once
#: ready and 503 until then. Same probe the healthcheck runs, reachable by a
#: caller that did not start the container and so cannot read docker's verdict.
READYZ_PORT = 9099
READYZ_PATH = "/readyz"

# The provider under test, pinned for the same reason the images are. An
# unpinned `source` resolves to whatever the registry serves that day, so a
# scenario's result would depend on the date it ran, and the parked write
# scenarios would keep citing a version nothing held them to.
#
# The source is fully qualified because the fork is published only to
# registry.terraform.io; the OpenTofu registry has no jamesbraid namespace.
# Both darwin_arm64 and linux_amd64 are published, so local Colima runs and
# the CI step both resolve.
PROVIDER_SOURCE = "registry.terraform.io/jamesbraid/unifi"
PROVIDER_VERSION = "0.101.2"
