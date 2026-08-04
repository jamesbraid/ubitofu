# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 James Braid
import httpx
import pytest

from ubitofu.config import Config
from ubitofu.controller import ABSENT_ENDPOINTS, Controller, controller_from_config
from ubitofu.errors import ControllerResponseError


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


def test_authentication_failure_is_typed_and_safe():
    def handler(request):
        return httpx.Response(401, json={"meta": {"rc": "error"}})

    with pytest.raises(ControllerResponseError) as exc_info:
        _client(handler).collection("rest/networkconf")
    assert exc_info.value.status == 401
    assert exc_info.value.endpoint_id == "rest/networkconf"


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


def test_classic_login_failure_is_typed():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"meta": {"rc": "error"}})

    ctl = Controller(base_url="https://c:8443", site="default", dialect="classic",
                     username="admin", password="bad",
                     transport=httpx.MockTransport(handler))
    with pytest.raises(ControllerResponseError):
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


def test_close_closes_http_client():
    c = Controller(base_url="https://unifi.example", site="default", api_key="KEY")
    assert not c._http.is_closed
    c.close()
    assert c._http.is_closed


def test_close_is_idempotent():
    c = Controller(base_url="https://unifi.example", site="default", api_key="KEY")
    c.close()
    c.close()  # must not raise
    assert c._http.is_closed


def test_factory_builds_unifi_os_with_resolved_key(monkeypatch):
    monkeypatch.setenv("KEY", "k123")
    cfg = Config(controller_url="https://udm", site="default",
                 api_key_source="env", api_key_ref="KEY")
    ctl = controller_from_config(cfg)
    assert (ctl.dialect, ctl.api_key) == ("unifi-os", "k123")


def test_controller_verifies_tls_by_default_and_allows_explicit_insecure():
    verified = Controller(base_url="https://unifi.example", site="default", api_key="KEY")
    insecure = Controller(
        base_url="https://unifi.example", site="default", api_key="KEY", verify_tls=False
    )
    assert verified.verify_tls is True
    assert insecure.verify_tls is False
    verified.close()
    insecure.close()


def test_controller_loads_custom_ca_bundle(monkeypatch, tmp_path):
    bundle = tmp_path / "controller-ca.pem"
    bundle.write_text("test bundle")
    seen = {}

    def fake_context(*, cafile):
        seen["cafile"] = cafile
        return False

    monkeypatch.setattr("ubitofu.controller.ssl.create_default_context", fake_context)
    ctl = Controller(
        base_url="https://unifi.example", site="default", api_key="KEY", ca_bundle=str(bundle)
    )
    assert seen["cafile"] == str(bundle)
    ctl.close()


def test_controller_rejects_insecure_mode_with_custom_ca_bundle(tmp_path):
    bundle = tmp_path / "controller-ca.pem"
    bundle.write_text("test bundle")
    with pytest.raises(ValueError, match="ca_bundle"):
        Controller(
            base_url="https://unifi.example", site="default", api_key="KEY",
            verify_tls=False, ca_bundle=str(bundle),
        )


@pytest.mark.parametrize("status", [404, 405])
def test_unlisted_endpoint_absence_is_an_operational_error(status):
    def handler(request):
        return httpx.Response(status, json={"data": []})

    with pytest.raises(ControllerResponseError) as exc_info:
        _client(handler).collection("rest/unlisted")
    assert exc_info.value.status == status


def test_policy_listed_absence_is_reported_as_endpoint_absent():
    endpoint, dialect = next(iter(ABSENT_ENDPOINTS))

    def handler(request):
        return httpx.Response(next(iter(ABSENT_ENDPOINTS[(endpoint, dialect)])), json={"data": []})

    ctl = Controller(
        base_url="https://unifi.example", site="default", api_key="KEY", dialect=dialect,
        transport=httpx.MockTransport(handler),
    )
    assert ctl.collection(endpoint) == []


def test_rate_limited_get_retries_only_to_the_limit(monkeypatch):
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        return httpx.Response(429, headers={"retry-after": "0"}, json={"data": []})

    monkeypatch.setattr("ubitofu.controller.time.sleep", lambda _: None)
    with pytest.raises(ControllerResponseError, match="rate limited"):
        _client(handler).collection("rest/networkconf")
    assert calls == 3


@pytest.mark.parametrize(
    "body", [{"data": "not-a-list"}, {"meta": "not-an-object"}, "not-an-object"]
)
def test_collection_rejects_malformed_response_envelopes(body):
    def handler(request):
        return httpx.Response(200, json=body)

    with pytest.raises(ControllerResponseError):
        _client(handler).collection("rest/networkconf")
