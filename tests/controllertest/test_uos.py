# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 James Braid
"""Unit tests for uos.native_api_key's 401/403 body-code gate.

Offline — no docker/live controller needed (unmarked, runs in the default
suite). httpx.Client inside uos.py is monkeypatched to route through
httpx.MockTransport so every branch of the documented decision (see uos.py's
module docstring for the full probe transcript) is exercised without a real
UOS instance:

  401/403 AND body["code"] == "AUTHENTICATION_FAILED_NTP_OUT_OF_SYNC"
      -> None (the one documented condition)
  401/403 with any other code (or no code at all)
      -> raise RuntimeError (credential rot, or a future image fix, must
         surface loudly rather than being misread as the known gap)
  401/403 with an unparseable body
      -> raise RuntimeError (unknown territory)
"""
import httpx
import pytest

from . import uos as uos_module
from .uos import native_api_key

_NTP_BODY = {
    "message": "Authentication failed, NTP out of sync",
    "code": "AUTHENTICATION_FAILED_NTP_OUT_OF_SYNC",
    "level": "debug",
}


def _patch_client(monkeypatch, handler):
    real_client = httpx.Client  # capture before patching — uos_module.httpx IS this module

    def fake_client(*, base_url, verify, timeout):  # noqa: ARG001 — matches httpx.Client's shape
        return real_client(transport=httpx.MockTransport(handler), base_url=base_url)

    monkeypatch.setattr(uos_module.httpx, "Client", fake_client)


def test_returns_none_on_401_with_documented_ntp_code(monkeypatch):
    def handler(request):
        return httpx.Response(401, json=_NTP_BODY)

    _patch_client(monkeypatch, handler)
    assert native_api_key("https://x", "admin", "admin") is None


def test_returns_none_on_403_with_documented_ntp_code(monkeypatch):
    def handler(request):
        return httpx.Response(403, json=_NTP_BODY)

    _patch_client(monkeypatch, handler)
    assert native_api_key("https://x", "admin", "admin") is None


def test_raises_on_401_with_different_code(monkeypatch):
    # Credential rot after a future image fixes NTP: a real auth failure
    # must not be misread as the documented gap.
    def handler(request):
        return httpx.Response(401, json={"message": "Invalid credentials",
                                         "code": "INVALID_PASSWORD"})

    _patch_client(monkeypatch, handler)
    with pytest.raises(RuntimeError, match="401"):
        native_api_key("https://x", "admin", "admin")


def test_raises_on_401_with_no_code_field(monkeypatch):
    def handler(request):
        return httpx.Response(401, json={"message": "Unauthorized"})

    _patch_client(monkeypatch, handler)
    with pytest.raises(RuntimeError, match="401"):
        native_api_key("https://x", "admin", "admin")


def test_raises_on_403_with_different_code(monkeypatch):
    def handler(request):
        return httpx.Response(403, json={"message": "Forbidden", "code": "SOME_OTHER_CODE"})

    _patch_client(monkeypatch, handler)
    with pytest.raises(RuntimeError, match="403"):
        native_api_key("https://x", "admin", "admin")


def test_raises_on_401_unparseable_body(monkeypatch):
    def handler(request):
        return httpx.Response(401, content=b"not json", headers={"content-type": "text/plain"})

    _patch_client(monkeypatch, handler)
    with pytest.raises(RuntimeError, match="401"):
        native_api_key("https://x", "admin", "admin")


def test_raises_on_403_unparseable_body(monkeypatch):
    def handler(request):
        return httpx.Response(403, content=b"not json", headers={"content-type": "text/plain"})

    _patch_client(monkeypatch, handler)
    with pytest.raises(RuntimeError, match="403"):
        native_api_key("https://x", "admin", "admin")


def test_raises_on_unexpected_status(monkeypatch):
    # Regression guard: unrelated to the 401/403 gate, must stay unaffected.
    def handler(request):
        return httpx.Response(500, text="boom")

    _patch_client(monkeypatch, handler)
    with pytest.raises(RuntimeError, match="500"):
        native_api_key("https://x", "admin", "admin")


def test_empty_native_url_returns_none():
    assert native_api_key("", "admin", "admin") is None
