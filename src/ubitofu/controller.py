# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 James Braid
import os
from dataclasses import dataclass, field

import httpx

from .config import Config, resolve_api_key, resolve_password


@dataclass
class Controller:
    base_url: str
    site: str
    api_key: str = ""
    # "unifi-os": /proxy/network prefix + X-API-KEY (UDM, Cloud Key, UOS).
    # "classic": unprefixed paths + cookie login (standalone Network app).
    dialect: str = "unifi-os"
    username: str = ""
    password: str = ""
    verify_tls: bool = False
    transport: httpx.BaseTransport | None = field(default=None, repr=False)
    _http: httpx.Client = field(init=False, repr=False)
    _logged_in: bool = field(init=False, default=False, repr=False)

    def __post_init__(self) -> None:
        if self.dialect not in ("unifi-os", "classic"):
            raise ValueError(f"unknown dialect: {self.dialect!r}")
        kwargs: dict[str, object] = {"base_url": self.base_url, "verify": self.verify_tls}
        if self.transport is not None:
            kwargs["transport"] = self.transport
        self._http = httpx.Client(**kwargs)  # type: ignore[arg-type]

    def close(self) -> None:
        """Close the underlying http client. Idempotent — safe to call twice.

        Callers that construct a Controller (cli, pipeline) own its
        lifetime and must close it when done, or every run leaks a socket
        into the process (the in-process test harness accumulates them
        across a whole suite run).
        """
        self._http.close()

    def _resolve(self, endpoint: str) -> str:
        endpoint = endpoint.replace("{site}", self.site)
        prefix = "" if self.dialect == "classic" else "/proxy/network"
        if endpoint.startswith("v2/") or endpoint.startswith("api/self"):
            return f"{prefix}/{endpoint}"
        return f"{prefix}/api/s/{self.site}/{endpoint}"

    def _ensure_login(self) -> None:
        if self.dialect != "classic" or self._logged_in:
            return
        resp = self._http.post(
            "/api/login", json={"username": self.username, "password": self.password}
        )
        resp.raise_for_status()
        self._logged_in = True

    def get(self, path: str) -> object:
        self._ensure_login()
        headers = {"Accept": "application/json"}
        if self.api_key:
            headers["X-API-KEY"] = self.api_key
        resp = self._http.get(self._resolve(path), headers=headers)
        resp.raise_for_status()
        return resp.json()

    def collection(self, endpoint: str) -> list[dict]:  # type: ignore[type-arg]
        body = self.get(endpoint)
        if isinstance(body, dict) and "data" in body:
            return list(body["data"])
        if isinstance(body, list):
            return list(body)
        return [body] if isinstance(body, dict) else []


def controller_from_config(cfg: Config) -> Controller:
    """The single Controller construction path for cli and pipeline."""
    if cfg.dialect == "classic":
        return Controller(
            base_url=cfg.controller_url, site=cfg.site, dialect="classic",
            username=cfg.username,
            password=resolve_password(cfg, environ=os.environ),
        )
    return Controller(
        base_url=cfg.controller_url, site=cfg.site,
        api_key=resolve_api_key(cfg, environ=os.environ),
    )
