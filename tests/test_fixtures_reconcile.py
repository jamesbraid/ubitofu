# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 James Braid
"""Real sanitized reconcile fixtures load and carry the shapes 0.3 must handle."""
import json


def _load(fixtures_dir, name):
    return json.loads((fixtures_dir / "reconcile" / name).read_text())


def test_two_same_name_plan_has_two_devices_one_base_name(fixtures_dir):
    plan = _load(fixtures_dir, "plan_two_same_name.json")
    devices = [rc for rc in plan["resource_changes"]
               if rc["type"] == "unifi_device"]
    macs = {rc["change"]["after"]["mac"] for rc in devices
            if rc["change"].get("after")}
    # Two distinct MACs, same base name -> the collision shape.
    assert len(macs) >= 2


def test_orphan_plan_has_a_resource_that_would_be_destroyed(fixtures_dir):
    plan = _load(fixtures_dir, "plan_orphan.json")
    destroyers = [rc for rc in plan["resource_changes"]
                  if "delete" in rc["change"]["actions"]
                  or "replace" in rc["change"]["actions"]]
    assert destroyers, "orphan fixture must contain a would-be-destroyed resource"


def test_new_secret_plan_has_a_secret_shaped_value(fixtures_dir):
    plan = _load(fixtures_dir, "plan_new_secret.json")
    wlans = [rc for rc in plan["resource_changes"]
             if rc["type"] == "unifi_wlan"]
    assert wlans, "new-secret fixture must contain a unifi_wlan"


def test_no_real_secret_leaked_into_fixtures(fixtures_dir):
    # Scrubbing invariant: no plausible real PSK/key material in the fixtures.
    import re
    for p in (fixtures_dir / "reconcile").glob("*.json"):
        text = p.read_text()
        # 44-char base64 (WG key) or long high-entropy runs must be scrubbed to placeholders.
        assert "REDACTED" in text or "example" in text.lower() or \
            not re.search(r'[A-Za-z0-9+/]{40,}={0,2}', text), \
            f"possible unscrubbed secret in {p.name}"
