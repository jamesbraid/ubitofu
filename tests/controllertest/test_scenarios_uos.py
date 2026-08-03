# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 James Braid
"""UOS-native scenarios: ubitofu's PRODUCTION dialect (/proxy/network +
X-API-KEY on 443) against a live UniFi OS Server.

S11 (native dialect round-trip) runs against the owner-seeded UOS, which
bakes a working X-API-KEY at /unifi/api-key — closing the gap the Task 14
probe found on the -sim image, whose SSO login is gated on an NTP-sync
check that never passes under the container capability contract (the full
transcript is in uos.py's module docstring; native_api_key still documents
that -sim reality). The fallback the spec named — bake a key into the
image — is what shipped.

S12 (write/apply) is out of scope by controller decision — all write
scenarios are parked on the ubiquiti-community/unifi provider import bugs
(docs/provider-import-bugs.md), which S12's apply would hit identically; it
was never attempted here."""
import os

import pytest

from .readiness import login_client
from .sandbox import native_workspace
from .support import unavailable

pytestmark = [pytest.mark.controller, pytest.mark.uos]


def test_s0_uos_smoke_version(uos_controller):
    # Readiness already proven by the fixture (healthcheck / login poll on
    # the 7443 network app). Version enforcement, per-flavor env — NOT the
    # shared UNIFI_TEST_EXPECT_VERSION other flavors' S0 reads (testing
    # contract: one env var per flavor lineage).
    with login_client(uos_controller.base_url, uos_controller.username,
                      uos_controller.password) as client:
        body = client.get(f"/api/s/{uos_controller.site}/stat/sysinfo").json()
    live_network = str(body["data"][0]["version"])
    assert live_network  # the bundled network app answers with a version
    # UNIFI_TEST_UOS_EXPECT_VERSION means the version reported by the UOS
    # bundle's NETWORK APP on 7443 — NOT the UOS platform version
    # (pins.UOS_VERSION): the probe found no route on 443 that reports the
    # platform version pre-login, and the one that would (the SSO/portal
    # session) is exactly what S11 documents as unreachable headlessly.
    # For 5.1.21-sim there is no pin to default against here (see below) —
    # the live test's own observed value IS the value; report it rather
    # than assert a specific pin. Observed during verification: "10.4.57"
    # (coincidentally == pins.NETWORK_VERSION today — coincidence, not a
    # guarantee; see the no-default rationale below).
    expected = os.environ.get("UNIFI_TEST_UOS_EXPECT_VERSION")
    if expected is not None:
        assert live_network == expected, (
            f"live UOS-bundled network app reports {live_network}, "
            f"expected {expected} — stale or mistagged image, or pin drift"
        )
    # Container mode with the env unset: deliberately do NOT default to
    # pins.NETWORK_VERSION here. The non-empty assertion above is already
    # the whole check — the UOS image's bundled network-app version is
    # that image's own business and moves independently of the standalone
    # network image's pin; a coincidental match today would rot into a
    # false failure (bundle updates) or a false pass (masks real drift)
    # the moment the two diverge.
    # UOS platform version (pins.UOS_VERSION) enforcement is intentionally
    # NOT wired up here for the same pre-login-route reason above; that
    # constant stays the image-tag pin only (see pins.py / support.UOS).
    # The bundled-network-app version above is the readiness half of the
    # smoke either way, matching the other flavors' S0.


def test_s11_native_dialect_roundtrip(uos_seeded_controller, capsys, tmp_path, monkeypatch):
    # Production unifi-os dialect (/proxy/network + X-API-KEY) end to end
    # against a real UOS console. The owner-seeded image bakes a working
    # X-API-KEY at /unifi/api-key (its healthcheck gates on it), so this runs
    # headlessly with no SSO — closing, image-side, the gap the -sim image's
    # NTP-blocked login left. This scenario used to xfail on that gap.
    ctl = uos_seeded_controller
    if not ctl.api_key:
        # Container mode skips earlier (boot_flavor's key read); this is the
        # URL-mode case where UNIFI_TEST_UOS_SEEDED_KEY was not supplied.
        unavailable("seeded UOS exposed no baked X-API-KEY (set "
                    "UNIFI_TEST_UOS_SEEDED_KEY in URL mode)")

    cfg = native_workspace(tmp_path / "uos-wd", ctl, "default", monkeypatch)
    from ubitofu.cli import main
    code = main(["generate", "--config", str(cfg)])
    out = capsys.readouterr().out
    assert code == 0, out
    assert (cfg.parent / "generated.tf").exists()
