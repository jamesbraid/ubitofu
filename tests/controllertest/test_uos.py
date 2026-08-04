# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 James Braid
"""Unit tests for uos.native_api_key's 401/403 body-code gate.

Offline — no docker/live controller needed (unmarked, runs in the default
suite). httpx.Client inside uos.py is monkeypatched to route through
httpx.MockTransport so every branch of the documented decision (see uos.py's
module docstring for the full probe transcript) is exercised without a real
UOS instance:

  container mode AND 401/403 AND body["code"] is one of the two documented
  bootstrap rejection codes
      -> None
  default/external mode, or 401/403 with any other code (or no code at all)
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
_ACCOUNT_LOCKED_BODY = {
    "message": "Authentication failed, account locked",
    "code": "AUTHENTICATION_FAILED_ACCOUNT_LOCKED",
    "level": "debug",
}


def _patch_client(monkeypatch, handler):
    real_client = httpx.Client  # capture before patching — uos_module.httpx IS this module

    def fake_client(*, base_url, verify, timeout):  # noqa: ARG001 — matches httpx.Client's shape
        return real_client(transport=httpx.MockTransport(handler), base_url=base_url)

    monkeypatch.setattr(uos_module.httpx, "Client", fake_client)


@pytest.mark.parametrize(
    ("status_code", "body"),
    [
        (401, _NTP_BODY),
        (403, _NTP_BODY),
        (401, _ACCOUNT_LOCKED_BODY),
        (403, _ACCOUNT_LOCKED_BODY),
    ],
    ids=["401-ntp", "403-ntp", "401-account-locked", "403-account-locked"],
)
def test_container_mode_returns_none_for_documented_bootstrap_code(
    monkeypatch, status_code, body
):
    def handler(request):
        return httpx.Response(status_code, json=body)

    _patch_client(monkeypatch, handler)
    assert native_api_key(
        "https://x", "admin", "admin", container_mode=True
    ) is None


@pytest.mark.parametrize(
    ("status_code", "body"),
    [
        (401, _NTP_BODY),
        (403, _NTP_BODY),
        (401, _ACCOUNT_LOCKED_BODY),
        (403, _ACCOUNT_LOCKED_BODY),
    ],
    ids=["401-ntp", "403-ntp", "401-account-locked", "403-account-locked"],
)
def test_default_mode_raises_for_documented_container_bootstrap_code(
    monkeypatch, status_code, body
):
    def handler(request):
        return httpx.Response(status_code, json=body)

    _patch_client(monkeypatch, handler)
    with pytest.raises(RuntimeError, match=str(status_code)):
        native_api_key("https://x", "admin", "admin")


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
        native_api_key("https://x", "admin", "admin", container_mode=True)


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
