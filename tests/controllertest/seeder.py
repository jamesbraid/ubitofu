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

    # --- power supervisors (v2) --------------------------------------------
    def create_power_supervisor(self, site: str, client_mac: str) -> dict:
        """POST a power supervisor and return the created record.

        v2 endpoints answer with a bare object, not the classic
        ``{"meta": {...}, "data": [...]}`` envelope, so this cannot go through
        ``_call``. ``power_sources`` is deliberately empty: the controller
        resolves the upstream PoE port itself and fills it on read.
        """
        path = f"/v2/api/site/{site}/power-supervisors"
        body = {"client_mac": client_mac, "enabled": True, "power_sources": [],
                "settings": {"heartbeat_interval": 60, "silence_threshold": 900,
                             "power_off_duration": 120}}
        resp = self._client.post(path, json=body)
        try:
            payload = resp.json()
        except ValueError as exc:
            raise SeedError(
                f"POST {path}: non-JSON HTTP {resp.status_code}: {resp.text[:200]}"
            ) from exc
        if resp.status_code >= 400:
            raise SeedError(f"POST {path}: HTTP {resp.status_code}: {payload}")
        if not isinstance(payload, dict):
            raise SeedError(f"POST {path}: expected an object, got {payload!r}")
        return payload

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

    def adopt_device(self, site: str, mac: str) -> None:
        """Adopt a pending device so it becomes a real site-DB row.

        A power supervisor can only reference an adopted device: the
        controller 404s ``api.err.PowerConsumerDeviceNotFound`` otherwise.
        """
        self._call("POST", f"/api/s/{site}/cmd/devmgr", {"cmd": "adopt", "mac": mac})

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
