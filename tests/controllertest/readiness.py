# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 James Braid
"""URL-mode readiness per the controller testing contract.

Container mode never comes through here: testcontainers waits on the image's
docker healthcheck. This is for the other way a controller arrives — an
orchestrator started it and handed back a URL, which is how Woodpecker's
service containers work. Docker will not serve its health verdict over the
network, so that caller cannot read the one the image already computed.

The images answer that by serving the same verdict themselves, on
GET :9099/readyz — 200 once ready, 503 until then. When we know that URL,
asking it is the whole readiness check: it runs the identical probe the
healthcheck runs, which waits for the v2 surface and the full demo fleet as
well as the login. Re-deriving those here would be a second implementation
of someone else's contract, free to drift.

Without that URL — a run pointed at a real controller, or an image predating
the endpoint — fall back to the login poll: ready ⇔ POST /api/login answers
a JSON body with meta.rc == "ok". Weaker, and knowingly so; it goes green
before v2 and the fleet do. HTTP 200 alone is never sufficient, because
during early boot the controller serves an HTML placeholder on every path
with status 200. Connection errors and non-JSON bodies mean "still booting"
(retry); a JSON rc != "ok" is a real rejection and fails immediately.
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
    if "application/json" not in resp.headers.get("content-type", "").lower():
        return f"non-JSON HTTP {resp.status_code} (boot placeholder)"
    try:
        body = resp.json()
    except ValueError:
        return f"unparseable JSON body (HTTP {resp.status_code})"
    rc = body.get("meta", {}).get("rc")
    if rc != "ok":
        raise ReadinessError(f"login rejected: rc={rc!r} (HTTP {resp.status_code})")
    return None


def _readyz_probe(client: httpx.Client, url: str) -> str | None:
    """One /readyz read. None = ready; a string = retryable detail.

    Raises ReadinessError on a status the endpoint does not define, which
    means we are talking to something that is not it.
    """
    try:
        resp = client.get(url)
    except httpx.TransportError as exc:
        # The endpoint answers 503 from the moment the container starts
        # rather than refusing the connection, so this is the network still
        # coming up (or the wrong host) — not the contract's "not yet".
        return f"cannot connect: {exc}"
    if resp.status_code == 200:
        return None
    if resp.status_code == 503:
        return "503 (not ready yet)"
    raise ReadinessError(
        f"{url} answered HTTP {resp.status_code}; /readyz serves only 200 or 503"
    )


def wait_ready(
    base_url: str, username: str, password: str, *,
    timeout_s: float, interval_s: float = 3.0, ready_url: str = "",
) -> None:
    """Block until the controller is ready, or raise.

    With `ready_url` this is the image's own verdict and covers everything
    the healthcheck covers. Without it, the weaker login poll — see the
    module docstring for why that is not the same promise.

    Polling /readyz needs no client-side rate limiting: the probe behind it
    runs at most once every two seconds however fast callers ask, because
    its first stage is a login and UniFi rate-limits those globally.
    """
    deadline = time.monotonic() + timeout_s
    with httpx.Client(base_url=base_url, verify=False, timeout=10.0) as client:
        while True:
            if ready_url:
                detail = _readyz_probe(client, ready_url)
            else:
                detail = _probe(client, username, password)
            if detail is None:
                return
            if time.monotonic() >= deadline:
                where = ready_url or base_url
                raise ReadinessError(f"{where} not ready after {timeout_s}s: {detail}")
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
        if "application/json" not in resp.headers.get("content-type", "").lower() or not ok:
            raise ReadinessError(f"login failed: HTTP {resp.status_code}")
        cookies = resp.cookies
    return httpx.Client(base_url=base_url, verify=False, timeout=30.0, cookies=cookies)
