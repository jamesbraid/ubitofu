# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 James Braid
"""Single source of truth for controller test-target pins.

Contract: one version pin per flavor lineage; every image reference and
version expectation derives from these constants (env-overridable at the
fixture layer, never here).
"""

NETWORK_VERSION = "10.4.57"
UOS_VERSION = "5.1.21"
# unifi-emu, which releases the herder binary and the synthetic device image
# from one tag. The binary carries its matching image reference compiled in,
# so pinning the version pins both halves and neither can float.
EMU_VERSION = "0.5.1"

SEEDED_IMAGE = f"ghcr.io/jamesbraid/unifi-network:{NETWORK_VERSION}-seeded"
SIM_IMAGE = f"ghcr.io/jamesbraid/unifi-network:{NETWORK_VERSION}-sim"
UOS_IMAGE = f"ghcr.io/jamesbraid/unifi-os-server:{UOS_VERSION}-sim"
EMU_SYNTHETIC_IMAGE = f"ghcr.io/jamesbraid/unifi-emu:{EMU_VERSION}"
# The owner-seeded UOS variant: headless 443 login (unifi-core /api/setup),
# real empty site, no 7443 direct port. Its own flavor, distinct from the
# -sim UOS above — the native-dialect (443) test target.
UOS_SEEDED_IMAGE = f"ghcr.io/jamesbraid/unifi-os-server:{UOS_VERSION}-seeded"

# The provider under test. Pinned for the same reason the images are: an
# unpinned `source` resolves to whatever the registry serves that day, so a
# scenario's result would silently depend on the run date — and the parked
# write scenarios name a version they were never actually held to.
#
# The fork carries fixes ubitofu's scenarios depend on and is published only
# to registry.terraform.io (the OpenTofu registry has no jamesbraid
# namespace), so the source is fully qualified. Both darwin_arm64 and
# linux_amd64 are published: local Colima runs and the CI step both resolve.
PROVIDER_SOURCE = "registry.terraform.io/jamesbraid/unifi"
PROVIDER_VERSION = "0.101.1"
