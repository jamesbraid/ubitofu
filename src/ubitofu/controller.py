# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 James Braid
from dataclasses import dataclass, field

import httpx


@dataclass
class Controller:
    base_url: str
    site: str
    api_key: str
    verify_tls: bool = False
    _http: httpx.Client = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._http = httpx.Client(base_url=self.base_url, verify=self.verify_tls)

    def _resolve(self, endpoint: str) -> str:
        endpoint = endpoint.replace("{site}", self.site)
        if endpoint.startswith("v2/") or endpoint.startswith("api/self"):
            return f"/proxy/network/{endpoint}"
        return f"/proxy/network/api/s/{self.site}/{endpoint}"

    def get(self, path: str) -> object:
        resp = self._http.get(
            self._resolve(path),
            headers={"X-API-KEY": self.api_key, "Accept": "application/json"},
        )
        resp.raise_for_status()
        return resp.json()

    def collection(self, endpoint: str) -> list[dict]:  # type: ignore[type-arg]
        body = self.get(endpoint)
        if isinstance(body, dict) and "data" in body:
            return list(body["data"])
        if isinstance(body, list):
            return list(body)
        return [body] if isinstance(body, dict) else []
