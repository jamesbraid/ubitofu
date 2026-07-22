# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 James Braid
"""Thin, independent seeding client — deliberately NOT the code under test.

Contract: a failed seed fails the scenario; it must never decay into an
empty-collection skip (a fresh controller has empty collections for nearly
everything — a gate that skips on empty is vacuously green).
"""
import time
from typing import Any

import httpx

from .readiness import login_client
from .support import RunningController


class SeedError(Exception):
    pass


class Seeder:
    def __init__(self, ctl: RunningController) -> None:
        self._client: httpx.Client = login_client(ctl.base_url, ctl.username, ctl.password)

    def close(self) -> None:
        self._client.close()

    def _call(self, method: str, path: str, body: dict | None = None) -> list[dict]:
        resp = self._client.request(method, path, json=body)
        try:
            payload: dict[str, Any] = resp.json()
        except ValueError as exc:
            raise SeedError(f"{method} {path}: non-JSON HTTP {resp.status_code}") from exc
        if resp.status_code >= 400 or payload.get("meta", {}).get("rc") != "ok":
            raise SeedError(f"{method} {path}: HTTP {resp.status_code}: {payload.get('meta')}")
        return list(payload.get("data", []))

    # --- sites -----------------------------------------------------------
    def add_site(self, desc: str) -> str:
        data = self._call("POST", "/api/s/default/cmd/sitemgr",
                          {"cmd": "add-site", "desc": desc})
        if not data or "name" not in data[0]:
            raise SeedError(f"add-site returned no site payload: {data!r}")
        return str(data[0]["name"])

    # --- networks ---------------------------------------------------------
    def create_network(self, site: str, name: str, *, vlan: int, subnet: str,
                       **extra: object) -> dict:
        body: dict[str, object] = {
            "name": name, "purpose": "corporate",
            "vlan_enabled": True, "vlan": vlan,
            "ip_subnet": subnet, "dhcpd_enabled": False,
        }
        body.update(extra)
        data = self._call("POST", f"/api/s/{site}/rest/networkconf", body)
        if not data:
            raise SeedError("create_network: empty data")
        return data[0]

    def update_network(self, site: str, network_id: str, patch: dict) -> dict:
        data = self._call("PUT", f"/api/s/{site}/rest/networkconf/{network_id}", patch)
        if not data:
            raise SeedError("update_network: empty data")
        return data[0]

    def delete_network(self, site: str, network_id: str) -> None:
        self._call("DELETE", f"/api/s/{site}/rest/networkconf/{network_id}")

    def list_networks(self, site: str) -> list[dict]:
        return self._call("GET", f"/api/s/{site}/rest/networkconf")

    # --- readiness probes ---------------------------------------------------
    def v2_status(self, site: str) -> int:
        """Raw HTTP status of the v2 surface probe (firewall-policies).

        The sim controller's v2 endpoints lag v1 readiness after boot
        (500s while ZBF defaults materialize); scenario setup gates on
        this instead of retrying around the code under test.
        """
        return self._client.get(f"/v2/api/site/{site}/firewall-policies").status_code

    # --- devices ----------------------------------------------------------
    def list_devices(self, site: str) -> list[dict]:
        return self._call("GET", f"/api/s/{site}/stat/device")

    def delete_device(self, site: str, mac: str) -> None:
        """Delete (forget) a device.

        Sim/demo-mode devices start pending-adoption (``adopted: false``,
        no ``_id`` — not a real site-DB row): ``cmd/sitemgr delete-device``
        only knows about adopted devices and 400s ``api.err.UnknownDevice``
        for anything else. Adopt first, mirroring the real-world "UI-adopted
        device later removed" case this seeds for. Immediately after adopt
        the device holds a transient busy lock (observed ~9-10s against the
        10.4.57-sim image) during which delete-device 400s
        ``api.err.DeviceBusy``; retry through that specific error rather
        than sleep-and-guess. Any other error is a real failure.
        """
        self._call("POST", f"/api/s/{site}/cmd/devmgr", {"cmd": "adopt", "mac": mac})
        deadline = time.monotonic() + 45.0
        while True:
            resp = self._client.post(f"/api/s/{site}/cmd/sitemgr",
                                      json={"cmd": "delete-device", "mac": mac})
            try:
                payload: dict[str, Any] = resp.json()
            except ValueError as exc:
                raise SeedError(
                    f"delete-device: non-JSON HTTP {resp.status_code}") from exc
            meta = payload.get("meta", {})
            if meta.get("rc") == "ok":
                return
            if meta.get("msg") != "api.err.DeviceBusy" or time.monotonic() >= deadline:
                raise SeedError(f"delete-device: HTTP {resp.status_code}: {meta}")
            time.sleep(1.0)
