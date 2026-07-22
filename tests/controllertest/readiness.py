# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 James Braid
"""URL-mode readiness per the controller testing contract.

Ready ⇔ POST /api/login answers a JSON body with meta.rc == "ok". HTTP 200
alone is never sufficient: during early boot the controller serves an HTML
placeholder on every path with status 200. Connection errors and non-JSON
bodies mean "still booting" (retry); a JSON rc != "ok" is a real rejection
and fails immediately.
"""
import time

import httpx


class ReadinessError(Exception):
    pass


def _probe(client: httpx.Client, username: str, password: str) -> str | None:
    """One login attempt. None = ready; a string = retryable detail.

    Raises ReadinessError on a real rejection.
    """
    try:
        resp = client.post("/api/login", json={"username": username, "password": password})
    except httpx.TransportError as exc:
        return f"cannot connect: {exc}"
    if "application/json" not in resp.headers.get("content-type", ""):
        return f"non-JSON HTTP {resp.status_code} (boot placeholder)"
    try:
        body = resp.json()
    except ValueError:
        return f"unparseable JSON body (HTTP {resp.status_code})"
    rc = body.get("meta", {}).get("rc")
    if rc != "ok":
        raise ReadinessError(f"login rejected: rc={rc!r} (HTTP {resp.status_code})")
    return None


def wait_ready(
    base_url: str, username: str, password: str, *,
    timeout_s: float, interval_s: float = 3.0,
) -> None:
    deadline = time.monotonic() + timeout_s
    detail = "no probe ran"
    with httpx.Client(base_url=base_url, verify=False, timeout=10.0) as client:
        while True:
            detail = _probe(client, username, password)
            if detail is None:
                return
            if time.monotonic() >= deadline:
                raise ReadinessError(f"not ready after {timeout_s}s: {detail}")
            time.sleep(interval_s)


def login_client(base_url: str, username: str, password: str) -> httpx.Client:
    """Cookie-authenticated client for harness-side probes and seeding.

    Logs in with a throwaway client and returns a fresh, unopened Client
    carrying the session cookies — safe to use bare or as a context
    manager (httpx forbids re-opening a client that has already sent).
    """
    with httpx.Client(base_url=base_url, verify=False, timeout=30.0) as probe:
        resp = probe.post("/api/login", json={"username": username, "password": password})
        try:
            ok = resp.json().get("meta", {}).get("rc") == "ok"
        except ValueError:
            ok = False
        if "application/json" not in resp.headers.get("content-type", "") or not ok:
            raise ReadinessError(f"login failed: HTTP {resp.status_code}")
        cookies = resp.cookies
    return httpx.Client(base_url=base_url, verify=False, timeout=30.0, cookies=cookies)
