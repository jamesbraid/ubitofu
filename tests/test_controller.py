# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 James Braid
import httpx
import pytest

from ubitofu.config import Config
from ubitofu.controller import Controller, controller_from_config


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


def _transport(recorder: list[httpx.Request]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        recorder.append(request)
        if request.url.path == "/api/login":
            return httpx.Response(
                200, json={"meta": {"rc": "ok"}, "data": []},
                headers={"set-cookie": "unifises=abc123; Path=/"},
            )
        return httpx.Response(200, json={"meta": {"rc": "ok"}, "data": [{"_id": "x"}]})

    return httpx.MockTransport(handler)


def _classic(recorder):
    return Controller(
        base_url="https://c:8443", site="default", dialect="classic",
        username="admin", password="pw", transport=_transport(recorder),
    )


def test_classic_resolves_without_proxy_prefix():
    reqs: list[httpx.Request] = []
    _classic(reqs).collection("rest/networkconf")
    paths = [r.url.path for r in reqs]
    assert paths == ["/api/login", "/api/s/default/rest/networkconf"]


def test_classic_v2_path():
    reqs: list[httpx.Request] = []
    _classic(reqs).collection("v2/api/site/{site}/firewall-policies")
    assert reqs[-1].url.path == "/v2/api/site/default/firewall-policies"


def test_classic_logs_in_once_and_sends_cookie_not_api_key():
    reqs: list[httpx.Request] = []
    ctl = _classic(reqs)
    ctl.collection("rest/networkconf")
    ctl.collection("rest/wlanconf")
    logins = [r for r in reqs if r.url.path == "/api/login"]
    assert len(logins) == 1
    last = reqs[-1]
    assert "x-api-key" not in {k.lower() for k in last.headers}
    assert "unifises=abc123" in last.headers.get("cookie", "")


def test_classic_login_failure_raises_http_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"meta": {"rc": "error"}})

    ctl = Controller(base_url="https://c:8443", site="default", dialect="classic",
                     username="admin", password="bad",
                     transport=httpx.MockTransport(handler))
    with pytest.raises(httpx.HTTPStatusError):
        ctl.collection("rest/networkconf")


def test_unifi_os_dialect_unchanged():
    reqs: list[httpx.Request] = []
    ctl = Controller(base_url="https://udm", site="default", api_key="k",
                     transport=_transport(reqs))
    ctl.collection("rest/networkconf")
    assert reqs[0].url.path == "/proxy/network/api/s/default/rest/networkconf"
    assert reqs[0].headers["x-api-key"] == "k"


def test_unknown_dialect_rejected():
    with pytest.raises(ValueError, match="dialect"):
        Controller(base_url="https://c", site="default", dialect="udm")


def test_factory_builds_classic_with_resolved_password(monkeypatch):
    monkeypatch.setenv("PW", "s3cret")
    cfg = Config(controller_url="https://c:8443", site="s1", dialect="classic",
                 username="admin", password_source="env", password_ref="PW")
    ctl = controller_from_config(cfg)
    assert (ctl.dialect, ctl.username, ctl.password) == ("classic", "admin", "s3cret")
    assert ctl.api_key == ""


def test_factory_builds_unifi_os_with_resolved_key(monkeypatch):
    monkeypatch.setenv("KEY", "k123")
    cfg = Config(controller_url="https://udm", site="default",
                 api_key_source="env", api_key_ref="KEY")
    ctl = controller_from_config(cfg)
    assert (ctl.dialect, ctl.api_key) == ("unifi-os", "k123")
