# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 James Braid
"""Client-side adoption driver — the downstream half of the herder boundary.

The herder starts fake devices and hands back their identities. It is given
no credentials and does no adoption or controller polling at all, so driving
each MAC from "informing" to "connected" is this harness's job, and the
credentials stay here where the controller fixture already holds them.

Classic dialect only. The UOS-native path (443, session + rotating CSRF) is
deliberately not reimplemented here — it is fragile reverse-engineered ucore
behaviour, and a second Python copy of it would drift.

Adoption is a conversation, not a command:

  1. The device must exist on the controller first. `ready` means the
     container is healthy, not that its first inform landed, and adopting a
     MAC the controller has never seen is a command against nothing.
  2. `adopt` is then issued once and takes tens of seconds. Re-sending it on
     every poll restarts the process, so a re-issue waits out an interval.
  3. The controller may still answer api.err.CannotAdopt when the pending doc
     is only seconds old, and a rejected attempt can reap that doc until the
     next inform re-creates it. A human re-clicks Adopt; so does this. Every
     other rejection is fatal on the first answer.
  4. Connected means state 1 AND adopted: a device reports adopted while it
     is still provisioning, and scenarios need it actually up.
"""
import time

import httpx

from .readiness import login_client
from .support import RunningController

# Devices adopt serially at roughly half a minute each on a cold session
# controller; a small fleet needs real headroom.
ADOPT_TIMEOUT_S = 420.0
POLL_INTERVAL_S = 6.0
# How long an issued adopt is given before it is considered lost and re-sent.
READOPT_AFTER_S = 90.0

# Connected. Other values are transitional (2 pending, 5 provisioning) or
# broken (0 offline, 9 adoption failed) — none of them is "ready to assert on".
STATE_CONNECTED = 1

# Rejections that mean "not yet", not "no". This controller build answers an
# adopt against a pending doc that is only seconds old with api.err.CannotAdopt
# or api.err.CanNotAdoptUnknownDevice, and a rejected attempt can reap the doc
# until the device's next inform re-creates it. A human re-clicks Adopt.
# (Same behaviour go-unifi's internal/controllertest/adopt.go documents against
# these images.) Everything else is fatal on the first answer.
_RETRYABLE_ADOPT = ("cannotadopt",)


class AdoptionError(Exception):
    pass


def _request(client: httpx.Client, method: str, path: str, body: dict | None = None) -> dict:
    resp = client.request(method, path, json=body)
    try:
        payload = resp.json()
    except ValueError as exc:
        raise AdoptionError(f"{method} {path}: non-JSON HTTP {resp.status_code}") from exc
    if resp.status_code >= 400:
        raise AdoptionError(f"{method} {path}: HTTP {resp.status_code}: {payload.get('meta')}")
    return dict(payload)


def _call(client: httpx.Client, method: str, path: str, body: dict | None = None) -> list[dict]:
    payload = _request(client, method, path, body)
    if payload.get("meta", {}).get("rc") != "ok":
        raise AdoptionError(f"{method} {path}: {payload.get('meta')}")
    return list(payload.get("data", []))


def devices_by_mac(client: httpx.Client, site: str) -> dict[str, dict]:
    """Every device the controller holds, keyed by lowercase MAC."""
    docs = _call(client, "GET", f"/api/s/{site}/stat/device")
    return {str(doc.get("mac", "")).lower(): doc for doc in docs}


def send_adopt(client: httpx.Client, site: str, mac: str) -> str:
    """Issue one adopt command.

    Returns "" when the controller accepted it, or its message when the
    rejection is the retryable kind. Any other rejection raises: a device
    the site will never take (a second gateway, say) must fail now, not
    after the whole deadline.
    """
    payload = _request(client, "POST", f"/api/s/{site}/cmd/devmgr",
                       {"cmd": "adopt", "mac": mac})
    meta = payload.get("meta", {})
    if meta.get("rc") == "ok":
        return ""
    message = str(meta.get("msg", ""))
    if any(term in message.lower() for term in _RETRYABLE_ADOPT):
        return message
    raise AdoptionError(f"adopt {mac} rejected: {meta}")


def _connected(doc: dict) -> bool:
    return doc.get("state") == STATE_CONNECTED and bool(doc.get("adopted"))


def _describe(mac: str, doc: dict | None) -> str:
    if doc is None:
        return f"{mac}=absent"
    out = f"{mac}=state={doc.get('state')!r},adopted={bool(doc.get('adopted'))}"
    if doc.get("unsupported"):
        # This controller build never finishes adopting a model it flags
        # unsupported — the handshake ends at state=7. Say so, or the
        # timeout reads as a slow controller instead of a bad model.
        out += ",unsupported=True"
    return out


def drive_to_connected(
    client: httpx.Client,
    site: str,
    macs: list[str],
    *,
    timeout_s: float = ADOPT_TIMEOUT_S,
    interval_s: float = POLL_INTERVAL_S,
    readopt_after_s: float = READOPT_AFTER_S,
) -> None:
    """Adopt every MAC and block until all of them are connected."""
    want = [m.lower() for m in macs]
    issued: dict[str, float] = {}
    rejected: dict[str, str] = {}
    deadline = time.monotonic() + timeout_s
    seen: dict[str, dict] = {}
    while True:
        seen = devices_by_mac(client, site)
        pending = [m for m in want if not _connected(seen.get(m, {}))]
        if not pending:
            return
        now = time.monotonic()
        for mac in pending:
            if mac not in seen:
                # Not informing yet, or the doc was reaped. Either way the
                # last adopt plainly did not stick, so the next sighting is
                # adopted immediately rather than after the re-adopt wait.
                issued.pop(mac, None)
                continue
            if seen[mac].get("adopted"):
                # The command landed and the device is provisioning. The doc
                # outranks any stale rejection, and re-issuing now would
                # restart what is already working.
                issued[mac] = now
                continue
            last = issued.get(mac)
            if last is None or now - last >= readopt_after_s:
                message = send_adopt(client, site, seen[mac].get("mac", mac))
                issued[mac] = now
                if message:
                    # Retryable: try again next poll rather than waiting out
                    # the interval meant for an adopt that was accepted.
                    rejected[mac] = message
                    issued.pop(mac, None)
        if time.monotonic() >= deadline:
            detail = ", ".join(
                _describe(m, seen.get(m)) +
                (f" last-rejection={rejected[m]!r}" if m in rejected else "")
                for m in pending
            )
            raise AdoptionError(
                f"devices did not reach connected within {timeout_s}s: {detail}"
            )
        time.sleep(interval_s)


def adopt_fleet(
    controller: RunningController,
    macs: list[str],
    *,
    timeout_s: float = ADOPT_TIMEOUT_S,
    interval_s: float = POLL_INTERVAL_S,
) -> None:
    """Log in to the controller and drive the whole fleet to connected."""
    with login_client(controller.base_url, controller.username, controller.password) as client:
        drive_to_connected(client, controller.site, macs,
                           timeout_s=timeout_s, interval_s=interval_s)
