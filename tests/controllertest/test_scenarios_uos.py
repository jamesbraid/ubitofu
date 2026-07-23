# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 James Braid
"""UOS-native scenarios: ubitofu's PRODUCTION dialect (/proxy/network +
X-API-KEY on 443) against a live UniFi OS Server. S11 per the spec; the
Task 14 probe (see uos.py's module docstring for the full transcript)
found the -sim image cannot mint an API key headlessly — its SSO/portal
login (the only route to a session, hence to a mint endpoint) is gated on
an NTP-sync check that can never pass under the documented container
capability contract, regardless of credentials. S11 therefore xfails per
the spec's decision: the fallback is baking a pre-minted key into the
-sim image (a unifi-containers change), NOT teaching ubitofu UOS cookie
auth. S12 (write/apply) is out of scope by controller decision — all
write scenarios are parked on the ubiquiti-community/unifi provider
import bugs (docs/provider-import-bugs.md), which S12's apply would hit
identically; it was never attempted here."""
import os
import subprocess

import pytest

from .readiness import login_client
from .support import unavailable
from .uos import native_api_key

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


def test_s11_native_dialect_roundtrip(uos_controller, capsys, tmp_path, monkeypatch):
    if not uos_controller.native_url:
        if uos_controller.external:
            unavailable("UNIFI_TEST_UOS_NATIVE_URL is unset — no native "
                        "(443) endpoint to run S11 against")
        pytest.fail("uos_controller.native_url is empty in container mode "
                    "— 443 was not exposed/mapped by boot_flavor")

    key = native_api_key(uos_controller.native_url, uos_controller.username,
                         uos_controller.password)
    if key is None:
        pytest.xfail("UOS sim cannot mint an API key headlessly — "
                     "spec decision: bake a pre-minted key into the -sim image "
                     "(unifi-containers follow-up)")

    # Native config: the exact production shape — unifi-os dialect, API key.
    monkeypatch.setenv("UNIFI_TEST_UOS_KEY", key)
    workdir = tmp_path / "uos-wd"
    workdir.mkdir()
    cfg = workdir / "config.toml"
    cfg.write_text(
        f'controller_url = "{uos_controller.native_url}"\n'
        'site = "default"\n'
        'api_key_source = "env"\n'
        'api_key_ref = "UNIFI_TEST_UOS_KEY"\n'
        f'workdir = "{workdir}"\n'
    )
    (workdir / "providers.tf").write_text(
        'terraform {\n  required_providers {\n    unifi = {\n'
        '      source = "ubiquiti-community/unifi"\n    }\n  }\n}\n'
    )
    subprocess.run(["tofu", "init", "-input=false"], cwd=workdir, check=True,
                   capture_output=True)
    from ubitofu.cli import main
    code = main(["generate", "--config", str(cfg)])
    out = capsys.readouterr().out
    assert code == 0, out
    assert (workdir / "generated.tf").exists()
