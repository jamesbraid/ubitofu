# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 James Braid
"""Read-only controller adapter with explicit TLS and response policy."""

import os
import ssl
import time
from collections.abc import Mapping
from dataclasses import dataclass, field

import httpx

from .config import Config, resolve_api_key, resolve_password
from .errors import ControllerResponseError
from .values import FrozenObject, freeze_value

# An absence must be documented for this dialect and endpoint.  All other
# 404/405 responses are operational failures, never coverage evidence.
ABSENT_ENDPOINTS: Mapping[tuple[str, str], frozenset[int]] = {
    ("rest/hotspot2conf", "unifi-os"): frozenset({404}),
}
_MAX_GET_ATTEMPTS = 3


@dataclass(frozen=True)
class CollectionObservation:
    """One immutable collection read with policy absence kept distinct."""

    endpoint_id: str
    records: tuple[FrozenObject, ...]
    policy_absent: bool

    def __post_init__(self) -> None:
        if (
            not self.endpoint_id
            or not all(isinstance(record, FrozenObject) for record in self.records)
            or not isinstance(self.policy_absent, bool)
            or self.policy_absent
            and self.records
        ):
            raise ValueError("invalid collection observation")


@dataclass(frozen=True)
class _GetObservation:
    body: object
    policy_absent: bool


def _tls_verify(verify_tls: bool, ca_bundle: str) -> bool | ssl.SSLContext:
    if not verify_tls:
        if ca_bundle:
            raise ValueError("ca_bundle cannot be used when verify_tls is false")
        return False
    return ssl.create_default_context(cafile=ca_bundle or None)


@dataclass
class Controller:
    base_url: str
    site: str
    api_key: str = ""
    dialect: str = "unifi-os"
    username: str = ""
    password: str = ""
    verify_tls: bool = True
    ca_bundle: str = ""
    transport: httpx.BaseTransport | None = field(default=None, repr=False)
    _http: httpx.Client = field(init=False, repr=False)
    _logged_in: bool = field(init=False, default=False, repr=False)

    def __post_init__(self) -> None:
        if self.dialect not in ("unifi-os", "classic"):
            raise ValueError(f"unknown dialect: {self.dialect!r}")
        kwargs: dict[str, object] = {
            "base_url": self.base_url,
            "verify": _tls_verify(self.verify_tls, self.ca_bundle),
        }
        if self.transport is not None:
            kwargs["transport"] = self.transport
        self._http = httpx.Client(**kwargs)  # type: ignore[arg-type]

    def close(self) -> None:
        """Close the underlying client. Callers own every Controller."""
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
        try:
            resp = self._http.post(
                "/api/login", json={"username": self.username, "password": self.password}
            )
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            status = exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else None
            raise ControllerResponseError("login", status, "authentication failed") from exc
        self._logged_in = True

    def _get_observation(self, endpoint: str) -> _GetObservation:
        headers = {"Accept": "application/json"}
        if self.api_key:
            headers["X-API-KEY"] = self.api_key
        for attempt in range(_MAX_GET_ATTEMPTS):
            try:
                resp = self._http.get(self._resolve(endpoint), headers=headers)
            except httpx.HTTPError as exc:
                raise ControllerResponseError(endpoint, None, "controller request failed") from exc
            if resp.status_code in (404, 405) and resp.status_code in ABSENT_ENDPOINTS.get(
                (endpoint, self.dialect), frozenset()
            ):
                return _GetObservation([], True)
            if resp.status_code == 429:
                if attempt + 1 == _MAX_GET_ATTEMPTS:
                    raise ControllerResponseError(endpoint, 429, "rate limited")
                try:
                    delay = max(0.0, float(resp.headers.get("retry-after", "0")))
                except ValueError:
                    delay = 0.0
                time.sleep(delay)
                continue
            if resp.status_code in (401, 403):
                raise ControllerResponseError(endpoint, resp.status_code, "authentication failed")
            if resp.status_code >= 500:
                raise ControllerResponseError(endpoint, resp.status_code, "server error")
            if resp.status_code >= 400:
                raise ControllerResponseError(
                    endpoint, resp.status_code, "controller request failed"
                )
            try:
                return _GetObservation(resp.json(), False)
            except ValueError as exc:
                raise ControllerResponseError(
                    endpoint, resp.status_code, "invalid document"
                ) from exc
        raise AssertionError("unreachable retry loop")

    def _get(self, endpoint: str) -> object:
        return self._get_observation(endpoint).body

    def get(self, path: str) -> object:
        self._ensure_login()
        return self._get(path)

    def collection(self, endpoint: str) -> list[dict[str, object]]:
        return _collection_body(endpoint, self.get(endpoint))

    def collection_observation(self, endpoint: str) -> CollectionObservation:
        """Return one collection read without erasing accepted endpoint absence."""
        self._ensure_login()
        observation = self._get_observation(endpoint)
        records = _collection_body(endpoint, observation.body)
        frozen: list[FrozenObject] = []
        try:
            for record in records:
                value = freeze_value(record)
                if not isinstance(value, FrozenObject):
                    raise AssertionError("collection record did not freeze as an object")
                frozen.append(value)
        except ValueError as exc:
            raise ControllerResponseError(endpoint, 200, "invalid document") from exc
        return CollectionObservation(endpoint, tuple(frozen), observation.policy_absent)


def _collection_body(endpoint: str, body: object) -> list[dict[str, object]]:
    """Validate the raw controller envelope without applying domain policy."""
    if isinstance(body, list):
        if all(isinstance(item, dict) for item in body):
            return list(body)
        raise ControllerResponseError(endpoint, 200, "invalid collection envelope")
    if not isinstance(body, dict):
        raise ControllerResponseError(endpoint, 200, "invalid collection envelope")
    if "meta" in body and not isinstance(body["meta"], dict):
        raise ControllerResponseError(endpoint, 200, "invalid collection envelope")
    if "data" not in body:
        return [body]
    data = body["data"]
    if not isinstance(data, list) or not all(isinstance(item, dict) for item in data):
        raise ControllerResponseError(endpoint, 200, "invalid collection envelope")
    return list(data)


def controller_from_config(cfg: Config) -> Controller:
    """The single Controller construction path for cli and pipeline."""
    if cfg.dialect == "classic":
        return Controller(
            base_url=cfg.controller_url,
            site=cfg.site,
            dialect="classic",
            username=cfg.username,
            password=resolve_password(cfg, environ=os.environ),
            verify_tls=cfg.verify_tls,
            ca_bundle=cfg.ca_bundle,
        )
    return Controller(
        base_url=cfg.controller_url,
        site=cfg.site,
        api_key=resolve_api_key(cfg, environ=os.environ),
        verify_tls=cfg.verify_tls,
        ca_bundle=cfg.ca_bundle,
    )
