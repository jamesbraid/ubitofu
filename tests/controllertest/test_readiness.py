# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 James Braid
"""The boot-placeholder rule: during early boot the controller answers every
path — login included — with an HTML placeholder page and HTTP 200. Ready
means a JSON body with meta.rc == "ok"; nothing less."""
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from .readiness import ReadinessError, login_client, wait_ready


class _FlippingHandler(BaseHTTPRequestHandler):
    """Serves `responses` in order for POST /api/login; repeats the last."""

    responses: list[tuple[int, str, str]] = []  # (status, content_type, body)
    extra_headers: list[tuple[str, str]] = []  # sent with every response
    hits = 0

    def do_POST(self):  # noqa: N802 - http.server API
        cls = type(self)
        status, ctype, body = cls.responses[min(cls.hits, len(cls.responses) - 1)]
        cls.hits += 1
        payload = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(payload)))
        for name, value in cls.extra_headers:
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):  # silence
        pass


@pytest.fixture
def fake_login_server():
    def _serve(responses, extra_headers):
        handler = type("H", (_FlippingHandler,), {
            "responses": responses, "extra_headers": list(extra_headers), "hits": 0,
        })
        server = HTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        return f"http://127.0.0.1:{server.server_port}", server, handler

    servers = []

    def factory(responses, extra_headers=()):
        url, server, handler = _serve(responses, extra_headers)
        servers.append(server)
        return url, handler

    yield factory
    for s in servers:
        s.shutdown()
        s.server_close()


PLACEHOLDER = (200, "text/html", "<html><body>starting up</body></html>")
OK = (200, "application/json", json.dumps({"meta": {"rc": "ok"}, "data": []}))
REJECT = (400, "application/json", json.dumps({"meta": {"rc": "error", "msg": "api.err.Invalid"}}))


def test_http_200_html_placeholder_is_not_ready_then_json_ok_is(fake_login_server):
    url, handler = fake_login_server([PLACEHOLDER, PLACEHOLDER, OK])
    wait_ready(url, "admin", "admin", timeout_s=10, interval_s=0.01)
    assert handler.hits == 3  # retried through both placeholders


def test_json_rc_error_fails_immediately(fake_login_server):
    url, handler = fake_login_server([REJECT, OK])
    with pytest.raises(ReadinessError, match="rc"):
        wait_ready(url, "admin", "wrong", timeout_s=10, interval_s=0.01)
    assert handler.hits == 1  # no retry after a real rejection


def test_placeholder_forever_times_out_with_detail(fake_login_server):
    url, _ = fake_login_server([PLACEHOLDER])
    with pytest.raises(ReadinessError, match="placeholder|non-JSON"):
        wait_ready(url, "admin", "admin", timeout_s=0.05, interval_s=0.01)


def test_connection_refused_retries_until_timeout():
    with pytest.raises(ReadinessError, match="connect"):
        wait_ready("http://127.0.0.1:1", "admin", "admin", timeout_s=0.05, interval_s=0.01)


def test_login_client_success_returns_usable_client(fake_login_server):
    url, _ = fake_login_server([OK])
    client = login_client(url, "admin", "admin")
    assert str(client.base_url).rstrip("/") == url
    client.close()


def test_login_client_works_as_context_manager_with_cookies(fake_login_server):
    # httpx forbids re-opening a client that has already sent a request, so
    # the login POST must happen on a throwaway client — the returned one
    # must be unopened yet still carry the session cookies.
    url, _ = fake_login_server(
        [OK], extra_headers=[("Set-Cookie", "unifises=abc123; Path=/")]
    )
    with login_client(url, "admin", "admin") as client:
        assert not client.is_closed
        assert client.cookies.get("unifises") == "abc123"


def test_login_client_rejection_raises_and_closes(fake_login_server):
    url, _ = fake_login_server([REJECT])
    with pytest.raises(ReadinessError, match="login failed"):
        login_client(url, "admin", "wrong")


def test_login_client_malformed_json_raises_readiness_error(fake_login_server):
    url, _ = fake_login_server([(200, "application/json", "{not json")])
    with pytest.raises(ReadinessError, match="login failed"):
        login_client(url, "admin", "admin")


def test_wait_ready_retries_malformed_json_then_succeeds(fake_login_server):
    url, handler = fake_login_server([(200, "application/json", "{not json"), OK])
    wait_ready(url, "admin", "admin", timeout_s=10, interval_s=0.01)
    assert handler.hits == 2


# ---------------------------------------------------------------------------
# Item 6: content-type checks must be case-insensitive. Some servers/proxies
# send "Application/JSON" or similar; a case-sensitive `in` check misreads a
# perfectly good ready response as a boot placeholder and either retries
# forever (wait_ready) or reports a false "login failed" (login_client).
# ---------------------------------------------------------------------------

_OK_BODY = json.dumps({"meta": {"rc": "ok"}, "data": []})


def test_wait_ready_recognizes_uppercase_content_type(fake_login_server):
    url, handler = fake_login_server([(200, "APPLICATION/JSON", _OK_BODY)])
    wait_ready(url, "admin", "admin", timeout_s=10, interval_s=0.01)
    assert handler.hits == 1  # ready on the first probe, no placeholder retry


def test_login_client_recognizes_mixed_case_content_type(fake_login_server):
    url, _ = fake_login_server([(200, "Application/Json", _OK_BODY)])
    client = login_client(url, "admin", "admin")
    client.close()
