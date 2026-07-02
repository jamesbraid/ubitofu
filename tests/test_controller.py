# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 James Braid
import httpx
import pytest

from ubitofu.controller import Controller


def _client(handler):
    transport = httpx.MockTransport(handler)
    c = Controller(base_url="https://unifi.example", site="default", api_key="KEY")
    c._http = httpx.Client(transport=transport, base_url="https://unifi.example")
    return c


def test_get_sends_api_key_and_accept_headers():
    seen = {}

    def handler(request):
        seen["auth"] = request.headers.get("x-api-key")
        seen["accept"] = request.headers.get("accept")
        seen["path"] = request.url.path
        return httpx.Response(200, json={"data": [{"_id": "1"}]})

    c = _client(handler)
    c.collection("rest/networkconf")
    assert seen["auth"] == "KEY"
    assert seen["accept"] == "application/json"
    assert seen["path"] == "/proxy/network/api/s/default/rest/networkconf"


def test_collection_unwraps_data_envelope():
    def handler(request):
        body = {"meta": {"rc": "ok"}, "data": [{"_id": "a"}, {"_id": "b"}]}
        return httpx.Response(200, json=body)

    assert len(_client(handler).collection("rest/networkconf")) == 2


def test_v2_endpoint_not_site_prefixed_and_bare_list():
    def handler(request):
        assert request.url.path == "/proxy/network/v2/api/site/default/firewall-policies"
        return httpx.Response(200, json=[{"_id": "x"}])

    out = _client(handler).collection("v2/api/site/{site}/firewall-policies")
    assert out == [{"_id": "x"}]


def test_client_exposes_no_write_verbs():
    # Global Constraint #1: GET-only. No mutation methods on the client.
    assert not hasattr(Controller, "post")
    assert not hasattr(Controller, "put")
    assert not hasattr(Controller, "delete")
    assert not hasattr(Controller, "patch")


def test_http_error_raises():
    def handler(request):
        return httpx.Response(401, json={"meta": {"rc": "error"}})

    with pytest.raises(httpx.HTTPStatusError):
        _client(handler).collection("rest/networkconf")
