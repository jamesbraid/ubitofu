# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 James Braid
"""Unit tests for the client-side adoption driver (unmarked — no docker).

The fake below is a small state machine, not a script: adoption is a
conversation (appear → adopt → connect), and a list of canned responses
would let a driver that asks the wrong questions still pass.
"""
import json

import httpx
import pytest

from .adopt import AdoptionError, drive_to_connected

SITE = "default"
MAC = "02:aa:bb:cc:dd:01"
OTHER = "02:aa:bb:cc:dd:99"


class FakeController:
    """stat/device + cmd/devmgr, with a device's real adoption lifecycle."""

    def __init__(self, *, present=(), appears_after=0, connect_after=1, adopt_rc="ok",
                 reject_first=0, reject_msg="api.err.CannotAdopt",
                 lands_anyway=False, reap_after_ok=0):
        self.docs = {m: {"mac": m, "state": 2, "adopted": False} for m in present}
        self.appears_after = appears_after
        self.connect_after = connect_after
        self.adopt_rc = adopt_rc
        # This controller build rejects an adopt against a pending doc that
        # is only seconds old, and a rejected attempt can reap the doc
        # entirely until the device's next inform re-creates it.
        self.reject_first = reject_first
        self.reject_msg = reject_msg
        self.lands_anyway = lands_anyway   # rejected, but it took effect anyway
        self.reap_after_ok = reap_after_ok  # doc vanishes again after an accepted adopt
        self.adopts: list[str] = []
        self.reaped: set[str] = set()
        self.polls = 0
        self._adopted_at: dict[str, int] = {}

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/cmd/devmgr"):
            body = json.loads(request.content)
            if body.get("cmd") != "adopt":
                return self._json({"meta": {"rc": "error", "msg": "unexpected cmd"}})
            mac = body["mac"]
            self.adopts.append(mac)
            if self.adopt_rc != "ok":
                return self._json({"meta": {"rc": "error", "msg": self.adopt_rc}, "data": []})
            if len(self.adopts) <= self.reject_first:
                if self.lands_anyway:
                    # The command took effect; only the rc was stale.
                    self.docs[mac] = {**self.docs[mac], "adopted": True}
                    self._adopted_at[mac] = self.polls
                return self._json({"meta": {"rc": "error", "msg": self.reject_msg}, "data": []})
            if self.reap_after_ok:
                # Accepted, then the device falls off and its doc is reaped
                # until the next inform re-creates it, still unadopted.
                self.reap_after_ok -= 1
                self.reaped.add(mac)
                return self._json({"meta": {"rc": "ok"}, "data": []})
            self._adopted_at[mac] = self.polls
            return self._json({"meta": {"rc": "ok"}, "data": []})

        self.polls += 1
        data = []
        if self.polls > self.appears_after:
            for mac, doc in self.docs.items():
                if mac in self.reaped:
                    # The next inform re-creates it; one poll later here.
                    self.reaped.discard(mac)
                    continue
                since = self._adopted_at.get(mac)
                if since is not None and self.polls - since >= self.connect_after:
                    doc = {**doc, "state": 1, "adopted": True}
                data.append(doc)
        return self._json({"meta": {"rc": "ok"}, "data": data})

    @staticmethod
    def _json(payload: dict) -> httpx.Response:
        return httpx.Response(200, json=payload)


def client_for(fake: FakeController) -> httpx.Client:
    return httpx.Client(base_url="https://controller", transport=httpx.MockTransport(fake.handler))


def drive(fake: FakeController, macs=(MAC,), **kw) -> None:
    with client_for(fake) as client:
        drive_to_connected(client, SITE, list(macs),
                           **{"timeout_s": 5.0, "interval_s": 0.0, **kw})


# --- the happy path ---------------------------------------------------

def test_a_pending_device_is_adopted_and_reaches_connected():
    fake = FakeController(present=[MAC])
    drive(fake)
    assert fake.adopts == [MAC]


def test_adoption_is_not_attempted_before_the_device_appears():
    # The herder's `ready` means the container is healthy, not that the
    # first inform landed. Adopting a MAC the controller has never seen is
    # a command against nothing.
    fake = FakeController(present=[MAC], appears_after=3)
    drive(fake)
    assert fake.adopts == [MAC]


def test_only_the_requested_macs_are_adopted():
    # A sim controller ships its own demo fleet; those devices are not ours
    # to touch.
    fake = FakeController(present=[MAC, OTHER])
    drive(fake, macs=[MAC])
    assert fake.adopts == [MAC]


def test_macs_match_case_insensitively():
    fake = FakeController(present=[MAC])
    drive(fake, macs=[MAC.upper()])
    assert fake.adopts  # the uppercase request found the lowercase doc


def test_adopt_is_issued_once_while_it_is_still_working():
    # Adoption takes tens of seconds. Re-sending the command on every poll
    # restarts it and the fleet never settles.
    fake = FakeController(present=[MAC], connect_after=6)
    drive(fake, readopt_after_s=60.0)
    assert fake.adopts == [MAC]


# --- the controller's own adoption races ------------------------------
#
# Behaviour go-unifi's controllertest/adopt.go documents against this same
# controller build: an adopt against a pending doc that is only seconds old
# comes back api.err.CannotAdopt / CanNotAdoptUnknownDevice, and a rejected
# attempt can reap the doc until the device's next inform re-creates it. A
# human re-clicks Adopt; so does this driver.

def test_cannotadopt_is_retried_rather_than_fatal():
    fake = FakeController(present=[MAC], reject_first=3)
    drive(fake, readopt_after_s=0.0)
    assert len(fake.adopts) == 4  # three rejections, then it took


def test_the_doc_outranks_the_rc_once_it_shows_adopted():
    # The command landed controller-side and the rejection is stale. The
    # device doc is the source of truth, so the driver stops re-issuing
    # instead of restarting a provisioning device on every poll.
    fake = FakeController(present=[MAC], reject_first=100, lands_anyway=True, connect_after=4)
    drive(fake, readopt_after_s=0.0)
    assert len(fake.adopts) == 1


def test_a_reaped_doc_is_readopted_as_soon_as_it_returns():
    # The adopt was accepted, then the device fell off and its doc was
    # reaped. Waiting out the re-adopt interval when it re-informs burns
    # the deadline for nothing: the accepted adopt plainly did not stick.
    fake = FakeController(present=[MAC], reap_after_ok=1)
    drive(fake, readopt_after_s=600.0)
    assert len(fake.adopts) == 2


# --- failure paths ----------------------------------------------------

def test_a_rejected_adopt_command_fails_loudly():
    fake = FakeController(present=[MAC], adopt_rc="api.err.NoSecondGateway")
    with pytest.raises(AdoptionError, match="NoSecondGateway"):
        drive(fake)


def test_a_device_that_never_connects_reports_its_last_seen_state():
    # A device stuck pending means the herder never informed or adoption
    # failed — that must not read as a merely slow controller.
    fake = FakeController(present=[MAC], connect_after=10_000)
    with pytest.raises(AdoptionError) as exc:
        drive(fake, timeout_s=0.2)
    assert MAC in str(exc.value)
    assert "state=2" in str(exc.value)


def test_a_model_the_controller_refuses_is_named_in_the_timeout():
    # This controller build never finishes adopting models it flags
    # unsupported — the handshake ends at state=7. Without saying so, the
    # timeout looks like a slow controller instead of a bad model choice.
    fake = FakeController(present=[MAC], connect_after=10_000)
    fake.docs[MAC] = {"mac": MAC, "state": 7, "adopted": True, "unsupported": True}
    with pytest.raises(AdoptionError, match="unsupported"):
        drive(fake, timeout_s=0.2)


def test_a_device_that_never_appears_is_named_as_absent():
    fake = FakeController(present=[], connect_after=1)
    with pytest.raises(AdoptionError) as exc:
        drive(fake, macs=[MAC], timeout_s=0.2)
    assert "absent" in str(exc.value)
    assert not fake.adopts


def test_adopted_alone_is_not_connected():
    # state 1 AND adopted: a device can report adopted while still
    # provisioning, and scenarios need it actually up.
    fake = FakeController(present=[MAC], connect_after=10_000)
    fake.docs[MAC] = {"mac": MAC, "state": 5, "adopted": True}
    with pytest.raises(AdoptionError, match="state=5"):
        drive(fake, timeout_s=0.2)
