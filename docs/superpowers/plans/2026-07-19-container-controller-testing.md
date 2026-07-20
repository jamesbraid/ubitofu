# Container-Controller Integration Testing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Live-controller E2E coverage for the v0.5.0 reconcile-errors work (surgeon depth-balance fix, classify_diverged, outcome exit codes) against published unifi-containers images, per the spec `docs/superpowers/specs/2026-07-19-container-controller-testing-design.md`.

**Architecture:** A `tests/controllertest/` harness (testcontainers-python, session fixtures per flavor: seeded/sim/UOS) behind a default-excluded `controller` pytest marker; a new classic dialect in `controller.py` (cookie login, unprefixed paths); scenarios drive `cli.main()` in-process and assert reports, files, and exit codes; CI = Woodpecker services workflow + dormant GHA workflow.

**Tech Stack:** Python 3.11, pytest, httpx, testcontainers>=4.14, OpenTofu CLI, `ubiquiti-community/unifi` provider, images from `ghcr.io/jamesbraid/unifi-containers`.

## Global Constraints

- **Normative contract:** `docs/testing-contract.md` on the `testing-contract` branch of github.com/jamesbraid/unifi-containers. Env vars are `UNIFI_TEST_*` exactly as used below. Where in doubt, the contract wins.
- **Image pins (single source: `tests/controllertest/pins.py`):** network `10.4.57`, UOS `5.1.21`. Images: `ghcr.io/jamesbraid/unifi-network:10.4.57-seeded` (admin / `unifi-containers-seeded`), `ghcr.io/jamesbraid/unifi-network:10.4.57-sim` (admin/admin), `ghcr.io/jamesbraid/unifi-os-server:5.1.21-sim` (admin/admin). Pull only — never build; missing tag is a hard failure.
- **Readiness (URL mode):** ready ⇔ `POST /api/login` answers a JSON body with `meta.rc == "ok"`. HTTP 200 alone is NEVER ready (early boot serves HTML placeholders with 200). Non-JSON or connection error → retry; JSON `rc != "ok"` → fail immediately.
- **Seeding:** a failed seed fails the test — never a skip. Writes only on the seeded flavor; each write scenario gets a fresh site.
- **Markers:** `controller` (all container-backed tests), `uos` (additionally on UOS tests). Default addopts exclude `controller`.
- **Skip-vs-fail:** missing docker/URL → friendly `pytest.skip` locally; `UNIFI_TEST_REQUIRE` set (CI) → `pytest.fail`.
- Every new file starts with the repo's SPDX header (`# SPDX-License-Identifier: GPL-3.0-or-later` + `# Copyright (C) 2026 James Braid`).
- Line length 100 (ruff, rules E/F/I/UP/B). `mypy --strict` covers `src/ubitofu` only — production changes must pass it; test files are exempt but keep annotations where cheap.
- **Commits are kernel-style:** `subsystem: imperative summary` (subsystems in this repo: `tests:`, `config:`, `controller:`, `cli:`, `pipeline:`, `ci:`, `docs:`). NO conventional-commit prefixes (`feat:`, `fix:` are forbidden). End commit bodies with `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`.
- Run milestone verification with the local venv's pytest. Controller-marked runs need colima/docker up.

---

### Task 1: Harness scaffolding — dependency group, markers, pins

**Files:**
- Modify: `pyproject.toml`
- Create: `tests/controllertest/__init__.py`
- Create: `tests/controllertest/pins.py`
- Test: `tests/controllertest/test_pins.py`

**Interfaces:**
- Produces: `pins.NETWORK_VERSION: str`, `pins.UOS_VERSION: str`, `pins.SEEDED_IMAGE: str`, `pins.SIM_IMAGE: str`, `pins.UOS_IMAGE: str` — every later task reads image/version data ONLY from here.
- Produces: pytest markers `controller`, `uos`; default runs exclude `controller`.

- [ ] **Step 1: Write the failing test**

`tests/controllertest/test_pins.py` (note: NOT marked `controller` — it needs no docker and must run in the default suite as a pin-format tripwire):

```python
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 James Braid
import re

from tests.controllertest import pins


def test_versions_are_semver():
    assert re.fullmatch(r"\d+\.\d+\.\d+", pins.NETWORK_VERSION)
    assert re.fullmatch(r"\d+\.\d+\.\d+", pins.UOS_VERSION)


def test_images_derive_from_pins():
    assert pins.SEEDED_IMAGE == f"ghcr.io/jamesbraid/unifi-network:{pins.NETWORK_VERSION}-seeded"
    assert pins.SIM_IMAGE == f"ghcr.io/jamesbraid/unifi-network:{pins.NETWORK_VERSION}-sim"
    assert pins.UOS_IMAGE == f"ghcr.io/jamesbraid/unifi-os-server:{pins.UOS_VERSION}-sim"
```

The `from tests.controllertest import pins` import requires `tests/__init__.py`… which does not exist and must NOT be created (the suite is rootdir-based). Instead use a relative-path import that works under pytest's rootdir conftest loading: in `tests/controllertest/test_pins.py` use `from . import pins` — pytest imports test packages fine because we create `tests/controllertest/__init__.py`. If collection errors with "attempted relative import", fall back to `import pins`-style path manipulation is FORBIDDEN; instead add `tests/__init__.py` AND `tests/controllertest/__init__.py` (both empty apart from the SPDX header) — run the full suite to confirm nothing else breaks.

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/controllertest/test_pins.py -v -p no:cacheprovider`
Expected: FAIL/ERROR — `pins` module does not exist.

- [ ] **Step 3: Write pins + package init + pyproject wiring**

`tests/controllertest/__init__.py`:

```python
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 James Braid
```

`tests/controllertest/pins.py`:

```python
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 James Braid
"""Single source of truth for controller test-target pins.

Contract: one version pin per flavor lineage; every image reference and
version expectation derives from these constants (env-overridable at the
fixture layer, never here).
"""

NETWORK_VERSION = "10.4.57"
UOS_VERSION = "5.1.21"

SEEDED_IMAGE = f"ghcr.io/jamesbraid/unifi-network:{NETWORK_VERSION}-seeded"
SIM_IMAGE = f"ghcr.io/jamesbraid/unifi-network:{NETWORK_VERSION}-sim"
UOS_IMAGE = f"ghcr.io/jamesbraid/unifi-os-server:{UOS_VERSION}-sim"
```

`pyproject.toml` — add after the `dev` extra in `[project.optional-dependencies]` (the `dev = [...]` line):

```toml
controller = ["testcontainers>=4.14"]
```

and change `[tool.pytest.ini_options]` to:

```toml
[tool.pytest.ini_options]
testpaths = ["tests"]
addopts = "-q -m 'not controller'"
markers = [
    "controller: needs a live containerized UniFi controller (excluded by default)",
    "uos: UniFi OS Server flavor (subset of controller)",
]
```

- [ ] **Step 4: Run tests to verify pass + no default-suite regression**

Run: `python -m pytest tests/controllertest/test_pins.py -v`
Expected: 2 passed.

Run: `python -m pytest`
Expected: full suite green, same test count as before this task plus 2 (the pins tests run by default — they carry no marker).

Run: `pip install -e ".[dev,controller]"` (into the project venv)
Expected: installs `testcontainers`.

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml tests/controllertest/
git commit -m "tests: scaffold controllertest harness package"
```

---

### Task 2: URL-mode readiness poll with the rc==ok rule

**Files:**
- Create: `tests/controllertest/readiness.py`
- Test: `tests/controllertest/test_readiness.py`

**Interfaces:**
- Produces: `ReadinessError(Exception)`;
  `wait_ready(base_url: str, username: str, password: str, *, timeout_s: float, interval_s: float = 3.0) -> None`;
  `login_client(base_url: str, username: str, password: str) -> httpx.Client` (verify off, cookies set, base_url bound — callers `.get("/api/s/<site>/...")`).
- Consumes: nothing from earlier tasks.

- [ ] **Step 1: Write the failing tests** (fast, unmarked — they use a local fake server, no docker)

`tests/controllertest/test_readiness.py`:

```python
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 James Braid
"""The boot-placeholder rule: during early boot the controller answers every
path — login included — with an HTML placeholder page and HTTP 200. Ready
means a JSON body with meta.rc == "ok"; nothing less."""
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from .readiness import ReadinessError, wait_ready


class _FlippingHandler(BaseHTTPRequestHandler):
    """Serves `responses` in order for POST /api/login; repeats the last."""

    responses: list[tuple[int, str, str]] = []  # (status, content_type, body)
    hits = 0

    def do_POST(self):  # noqa: N802 - http.server API
        cls = type(self)
        status, ctype, body = cls.responses[min(cls.hits, len(cls.responses) - 1)]
        cls.hits += 1
        payload = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):  # silence
        pass


@pytest.fixture
def fake_login_server():
    def _serve(responses):
        handler = type("H", (_FlippingHandler,), {"responses": responses, "hits": 0})
        server = HTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        return f"http://127.0.0.1:{server.server_port}", server, handler

    servers = []

    def factory(responses):
        url, server, handler = _serve(responses)
        servers.append(server)
        return url, handler

    yield factory
    for s in servers:
        s.shutdown()


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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/controllertest/test_readiness.py -v`
Expected: ERROR — no module `readiness`.

- [ ] **Step 3: Implement readiness.py**

```python
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
    rc = resp.json().get("meta", {}).get("rc")
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
    """Cookie-authenticated client for harness-side probes and seeding."""
    client = httpx.Client(base_url=base_url, verify=False, timeout=30.0)
    resp = client.post("/api/login", json={"username": username, "password": password})
    if "application/json" not in resp.headers.get("content-type", "") \
            or resp.json().get("meta", {}).get("rc") != "ok":
        client.close()
        raise ReadinessError(f"login failed: HTTP {resp.status_code}")
    return client
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/controllertest/test_readiness.py -v`
Expected: 4 passed.

Run: `python -m pytest`
Expected: green (readiness tests are unmarked and now part of the default suite).

- [ ] **Step 5: Commit**

```bash
git add tests/controllertest/readiness.py tests/controllertest/test_readiness.py
git commit -m "tests: normative rc==ok readiness poll for controllertest"
```

---

### Task 3: Flavor support, seeded fixture, S0 seeded smoke

**Files:**
- Create: `tests/controllertest/support.py`
- Create: `tests/controllertest/conftest.py`
- Test: `tests/controllertest/test_smoke.py`

**Interfaces:**
- Consumes: `pins`, `readiness.wait_ready`, `readiness.login_client` (Tasks 1–2).
- Produces: `support.RunningController` dataclass — fields `base_url: str`, `username: str`, `password: str`, `site: str`, `external: bool`;
  `support.Flavor` dataclass and constants `support.SEEDED`, `support.SIM` (Task 7 adds `UOS`);
  `support.boot_flavor(flavor) -> Iterator[RunningController]` generator used by conftest fixtures;
  pytest fixture `seeded_controller` (session scope) yielding `RunningController`.
- Env knobs (contract names, exact): `UNIFI_TEST_SEEDED_URL`, `UNIFI_TEST_SEEDED_IMAGE`, `UNIFI_TEST_REQUIRE`, `UNIFI_TEST_KEEP`, `UNIFI_TEST_EXPECT_VERSION`.

- [ ] **Step 1: Write the failing smoke test**

`tests/controllertest/test_smoke.py`:

```python
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 James Braid
"""S0: per-flavor smoke — readiness plus version enforcement. A wrong,
stale, or mistagged image must fail, not warn (contract: Version
enforcement)."""
import os

import pytest

from . import pins
from .readiness import login_client

pytestmark = pytest.mark.controller


def _live_network_version(ctl) -> str:
    with login_client(ctl.base_url, ctl.username, ctl.password) as client:
        body = client.get(f"/api/s/{ctl.site}/stat/sysinfo").json()
    return str(body["data"][0]["version"])


def test_seeded_smoke_version(seeded_controller):
    live = _live_network_version(seeded_controller)
    expected = os.environ.get("UNIFI_TEST_EXPECT_VERSION")
    if expected is None and not seeded_controller.external:
        expected = pins.NETWORK_VERSION  # container mode: image derives from the pin
    if expected is not None:
        assert live == expected, (
            f"live controller reports {live}, expected {expected} — "
            "stale or mistagged image, or pin drift"
        )
```

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest tests/controllertest/test_smoke.py -m controller -v`
Expected: ERROR — fixture `seeded_controller` not found.

- [ ] **Step 3: Implement support.py and conftest.py**

`tests/controllertest/support.py`:

```python
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 James Braid
"""Flavor definitions and container/URL-mode boot for controller fixtures.

Skip-vs-fail per the contract: missing docker or URL env is a friendly
skip locally; with UNIFI_TEST_REQUIRE set (CI always sets it) the same
condition is a hard failure — no skip may satisfy a required check.
"""
import os
from collections.abc import Iterator
from dataclasses import dataclass

import pytest

from . import pins
from .readiness import ReadinessError, wait_ready


@dataclass(frozen=True)
class Flavor:
    name: str            # "seeded" | "sim" | "uos"
    image: str
    url_env: str         # UNIFI_TEST_<FLAVOR>_URL
    image_env: str       # UNIFI_TEST_<FLAVOR>_IMAGE
    username: str
    password: str
    port: int            # controller API port inside the container
    boot_timeout_s: float


@dataclass(frozen=True)
class RunningController:
    base_url: str
    username: str
    password: str
    site: str
    external: bool  # True in URL mode — never assert pin-derived facts then


SEEDED = Flavor(
    name="seeded", image=pins.SEEDED_IMAGE,
    url_env="UNIFI_TEST_SEEDED_URL", image_env="UNIFI_TEST_SEEDED_IMAGE",
    username="admin", password="unifi-containers-seeded",
    port=8443, boot_timeout_s=300,
)
SIM = Flavor(
    name="sim", image=pins.SIM_IMAGE,
    url_env="UNIFI_TEST_SIM_URL", image_env="UNIFI_TEST_SIM_IMAGE",
    username="admin", password="admin",
    port=8443, boot_timeout_s=300,
)


def unavailable(reason: str) -> None:
    """Contract skip-vs-fail knob."""
    if os.environ.get("UNIFI_TEST_REQUIRE"):
        pytest.fail(f"UNIFI_TEST_REQUIRE is set and {reason}", pytrace=False)
    pytest.skip(reason)


def _docker_available() -> bool:
    # Any exception (including hostless-machine construction crashes seen in
    # other language ports) means "unavailable" — explicit selection must
    # report a clean skip/fail, never a stack trace.
    try:
        from testcontainers.core.docker_client import DockerClient
        DockerClient().client.ping()
        return True
    except Exception:  # noqa: BLE001
        return False


def boot_flavor(flavor: Flavor, run_kwargs: dict | None = None) -> Iterator[RunningController]:
    url = os.environ.get(flavor.url_env)
    if url:
        base_url = url.rstrip("/")
        wait_ready(base_url, flavor.username, flavor.password,
                   timeout_s=flavor.boot_timeout_s)
        yield RunningController(base_url, flavor.username, flavor.password,
                                site="default", external=True)
        return

    if not _docker_available():
        unavailable(f"docker unavailable and {flavor.url_env} unset")

    from testcontainers.core.container import DockerContainer
    from testcontainers.core.wait_strategies import HealthcheckWaitStrategy

    image = os.environ.get(flavor.image_env, flavor.image)
    container = (
        DockerContainer(image, **(run_kwargs or {}))
        .with_exposed_ports(flavor.port)
        .waiting_for(HealthcheckWaitStrategy().with_startup_timeout(int(flavor.boot_timeout_s)))
    )
    try:
        container.start()
    except Exception as exc:
        tail = ""
        try:
            stdout, stderr = container.get_logs()
            tail = (stdout + stderr).decode(errors="replace")[-4000:]
        except Exception:  # noqa: BLE001
            pass
        raise ReadinessError(
            f"{flavor.name} container ({image}) never became healthy: {exc}\n"
            f"--- log tail ---\n{tail}"
        ) from exc

    base_url = (
        f"https://{container.get_container_host_ip()}:"
        f"{container.get_exposed_port(flavor.port)}"
    )
    try:
        yield RunningController(base_url, flavor.username, flavor.password,
                                site="default", external=False)
    finally:
        if os.environ.get("UNIFI_TEST_KEEP"):
            print(f"UNIFI_TEST_KEEP set — leaving {flavor.name} container running: {base_url}")
        else:
            container.stop()
```

`tests/controllertest/conftest.py`:

```python
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 James Braid
import pytest

from .support import SEEDED, SIM, boot_flavor


@pytest.fixture(scope="session")
def seeded_controller():
    yield from boot_flavor(SEEDED)


@pytest.fixture(scope="session")
def sim_controller():
    yield from boot_flavor(SIM)
```

(`sim_controller` is declared now so conftest stays stable; its first user
is Task 7.)

- [ ] **Step 4: Run the smoke live** (colima must be up)

Run: `python -m pytest tests/controllertest/test_smoke.py -m controller -v`
Expected: 1 passed in roughly 20–60 s (image pull on first run). If it fails on version mismatch, STOP — the pin or the published tag is wrong; do not loosen the assert.

Also verify the gating both ways:
- `python -m pytest` → the smoke test is NOT collected (deselected by addopts).
- `UNIFI_TEST_SEEDED_URL= DOCKER_HOST=tcp://127.0.0.1:1 python -m pytest tests/controllertest/test_smoke.py -m controller -v` → 1 skipped ("docker unavailable").
- Same command with `UNIFI_TEST_REQUIRE=1` prepended → 1 failed ("UNIFI_TEST_REQUIRE is set…").

- [ ] **Step 5: Commit**

```bash
git add tests/controllertest/support.py tests/controllertest/conftest.py tests/controllertest/test_smoke.py
git commit -m "tests: seeded controller fixture and S0 version-enforcing smoke"
```

---

### Task 4: Config — dialect fields and password resolution

**Files:**
- Modify: `src/ubitofu/config.py`
- Test: `tests/test_config.py` (new)

**Interfaces:**
- Produces: `Config` gains fields (exact, with defaults): `dialect: str = "unifi-os"`, `username: str = ""`, `password_source: str = ""`, `password_ref: str = ""`; existing `api_key_source`, `api_key_ref`, `op_vault` become defaulted to `""` (field ORDER unchanged).
- Produces: `resolve_password(cfg: Config, environ: Mapping[str, str], op_reader: Callable[[str], str] = _op_read) -> str` — mirrors `resolve_api_key`.

- [ ] **Step 1: Write the failing tests**

`tests/test_config.py`:

```python
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 James Braid
import pytest

from ubitofu.config import Config, load_config, resolve_password


def _cfg(**kw):
    base = dict(controller_url="https://c:8443", site="default")
    base.update(kw)
    return Config(**base)


def test_dialect_defaults_to_unifi_os():
    assert _cfg().dialect == "unifi-os"


def test_classic_fields_load_from_toml(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text(
        'controller_url = "https://c:8443"\n'
        'site = "default"\n'
        'dialect = "classic"\n'
        'username = "admin"\n'
        'password_source = "env"\n'
        'password_ref = "UNIFI_TEST_PASSWORD"\n'
    )
    cfg = load_config(str(p))
    assert (cfg.dialect, cfg.username) == ("classic", "admin")
    assert (cfg.password_source, cfg.password_ref) == ("env", "UNIFI_TEST_PASSWORD")


def test_resolve_password_env():
    cfg = _cfg(password_source="env", password_ref="PW")
    assert resolve_password(cfg, environ={"PW": "s3cret"}) == "s3cret"


def test_resolve_password_op():
    cfg = _cfg(password_source="op", password_ref="op://v/i/f")
    assert resolve_password(cfg, environ={}, op_reader=lambda ref: f"read:{ref}") == "read:op://v/i/f"


def test_resolve_password_unknown_source_raises():
    with pytest.raises(ValueError, match="password_source"):
        resolve_password(_cfg(), environ={})
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/test_config.py -v`
Expected: FAIL — unexpected keyword / missing `resolve_password`.

- [ ] **Step 3: Implement**

In `src/ubitofu/config.py`, change the dataclass fields (order preserved; the api-key trio gains `""` defaults so classic configs can omit them) and append `resolve_password`:

```python
@dataclass
class Config:
    controller_url: str
    site: str
    api_key_source: str = ""
    api_key_ref: str = ""
    op_vault: str = ""  # the operator's secret-manager vault, from config
    workdir: str = "."
    # Classic (self-hosted) dialect: cookie login instead of X-API-KEY.
    dialect: str = "unifi-os"
    username: str = ""
    password_source: str = ""
    password_ref: str = ""
```

(keep `__post_init__` exactly as-is), and:

```python
def resolve_password(
    cfg: Config,
    environ: Mapping[str, str],
    op_reader: Callable[[str], str] = _op_read,
) -> str:
    if cfg.password_source == "env":
        return environ[cfg.password_ref]
    if cfg.password_source == "op":
        return op_reader(cfg.password_ref)
    raise ValueError(f"unknown password_source: {cfg.password_source!r}")
```

- [ ] **Step 4: Verify**

Run: `python -m pytest tests/test_config.py -v` → 5 passed.
Run: `python -m pytest && mypy src && ruff check .` → all green. If any existing test constructed `Config` positionally past `site`, fix that call site to keywords — do not reorder fields.

- [ ] **Step 5: Commit**

```bash
git add src/ubitofu/config.py tests/test_config.py
git commit -m "config: classic-dialect fields and password resolution"
```

---

### Task 5: Controller — classic dialect (paths + cookie login)

**Files:**
- Modify: `src/ubitofu/controller.py`
- Test: `tests/test_controller.py` (append)

**Interfaces:**
- Produces: `Controller` fields become (exact order): `base_url: str`, `site: str`, `api_key: str = ""`, `dialect: str = "unifi-os"`, `username: str = ""`, `password: str = ""`, `verify_tls: bool = False`, `transport: httpx.BaseTransport | None = None` (test seam). Behavior: classic ⇒ no `/proxy/network` prefix, lazy one-time `POST /api/login` cookie auth, no `X-API-KEY` header when `api_key` is empty; `unifi-os` behavior byte-identical to today.
- Consumes: nothing new (httpx already a dependency).

- [ ] **Step 1: Write the failing tests** (append to `tests/test_controller.py`; follow its existing style)

```python
import httpx
import pytest

from ubitofu.controller import Controller


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
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/test_controller.py -v`
Expected: new tests FAIL (unexpected keyword `dialect`/`transport`).

- [ ] **Step 3: Implement in `src/ubitofu/controller.py`**

Replace the dataclass body (keep `collection` as-is):

```python
@dataclass
class Controller:
    base_url: str
    site: str
    api_key: str = ""
    # "unifi-os": /proxy/network prefix + X-API-KEY (UDM, Cloud Key, UOS).
    # "classic": unprefixed paths + cookie login (standalone Network app).
    dialect: str = "unifi-os"
    username: str = ""
    password: str = ""
    verify_tls: bool = False
    transport: httpx.BaseTransport | None = field(default=None, repr=False)
    _http: httpx.Client = field(init=False, repr=False)
    _logged_in: bool = field(init=False, default=False, repr=False)

    def __post_init__(self) -> None:
        if self.dialect not in ("unifi-os", "classic"):
            raise ValueError(f"unknown dialect: {self.dialect!r}")
        kwargs: dict[str, object] = {"base_url": self.base_url, "verify": self.verify_tls}
        if self.transport is not None:
            kwargs["transport"] = self.transport
        self._http = httpx.Client(**kwargs)  # type: ignore[arg-type]

    def _resolve(self, endpoint: str) -> str:
        endpoint = endpoint.replace("{site}", self.site)
        prefix = "" if self.dialect == "classic" else "/proxy/network"
        if endpoint.startswith("v2/") or endpoint.startswith("api/self"):
            return f"{prefix}/{endpoint}"
        return f"{prefix}/api/s/{self.site}/{endpoint}"

    def _ensure_login(self) -> None:
        if self.dialect != "classic" or self._logged_in:
            return
        resp = self._http.post(
            "/api/login", json={"username": self.username, "password": self.password}
        )
        resp.raise_for_status()
        self._logged_in = True

    def get(self, path: str) -> object:
        self._ensure_login()
        headers = {"Accept": "application/json"}
        if self.api_key:
            headers["X-API-KEY"] = self.api_key
        resp = self._http.get(self._resolve(path), headers=headers)
        resp.raise_for_status()
        return resp.json()
```

- [ ] **Step 4: Verify**

Run: `python -m pytest tests/test_controller.py -v` → all pass.
Run: `python -m pytest && mypy src && ruff check .` → green (the `FakeController` subclasses override `__init__` and are unaffected; if mypy complains about the `httpx.Client(**kwargs)` spread, construct explicitly: `httpx.Client(base_url=self.base_url, verify=self.verify_tls, transport=self.transport)` guarded by `if self.transport is None` branches).

- [ ] **Step 5: Commit**

```bash
git add src/ubitofu/controller.py tests/test_controller.py
git commit -m "controller: classic self-hosted dialect with cookie login"
```

---

### Task 6: One controller factory, wired through cli and pipeline, plus live classic smoke

**Files:**
- Modify: `src/ubitofu/controller.py` (append factory)
- Modify: `src/ubitofu/cli.py:57-59` (`_controller`)
- Modify: `src/ubitofu/pipeline.py:277-279` (`run_generate`), `src/ubitofu/pipeline.py:558` (`run_reconcile`), `src/ubitofu/pipeline.py:752-753` (remove `_api_key`)
- Test: `tests/test_controller.py` (append), `tests/controllertest/test_smoke.py` (append)

**Interfaces:**
- Produces: `controller_from_config(cfg: Config) -> Controller` in `src/ubitofu/controller.py` — THE single construction path; resolves the api key (unifi-os) or password (classic) at build time.
- Consumes: `resolve_api_key`, `resolve_password` (Task 4); `Controller` (Task 5).

- [ ] **Step 1: Write the failing unit tests** (append to `tests/test_controller.py`)

```python
from ubitofu.config import Config
from ubitofu.controller import controller_from_config


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
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/test_controller.py -v` → ImportError on `controller_from_config`.

- [ ] **Step 3: Implement and wire**

Append to `src/ubitofu/controller.py` (add `import os` and `from .config import Config, resolve_api_key, resolve_password` at the top — no import cycle: config imports nothing from this module):

```python
def controller_from_config(cfg: Config) -> Controller:
    """The single Controller construction path for cli and pipeline."""
    if cfg.dialect == "classic":
        return Controller(
            base_url=cfg.controller_url, site=cfg.site, dialect="classic",
            username=cfg.username,
            password=resolve_password(cfg, environ=os.environ),
        )
    return Controller(
        base_url=cfg.controller_url, site=cfg.site,
        api_key=resolve_api_key(cfg, environ=os.environ),
    )
```

- `src/ubitofu/cli.py`: make `_controller` a one-liner `return controller_from_config(cfg)` (import it from `.controller`; drop the now-unused `resolve_api_key` import if nothing else uses it).
- `src/ubitofu/pipeline.py`: in `run_generate` and `run_reconcile` replace `ctl = Controller(base_url=cfg.controller_url, site=cfg.site, api_key=_api_key(cfg))` with `ctl = controller_from_config(cfg)`; delete `_api_key` and its `resolve_api_key` import if now unused; import `controller_from_config` from `.controller`.

- [ ] **Step 4: Add the live classic smoke** (append to `tests/controllertest/test_smoke.py`)

```python
def test_classic_dialect_reads_live_controller(seeded_controller):
    from ubitofu.controller import Controller

    ctl = Controller(
        base_url=seeded_controller.base_url, site=seeded_controller.site,
        dialect="classic", username=seeded_controller.username,
        password=seeded_controller.password,
    )
    nets = ctl.collection("rest/networkconf")
    assert isinstance(nets, list)  # seeded controller answers the classic v1 API
```

- [ ] **Step 5: Verify everything**

Run: `python -m pytest && mypy src && ruff check .` → green.
Run: `python -m pytest tests/controllertest -m controller -v` → smoke + live classic pass.

- [ ] **Step 6: Commit**

```bash
git add src/ubitofu/controller.py src/ubitofu/cli.py src/ubitofu/pipeline.py tests/test_controller.py tests/controllertest/test_smoke.py
git commit -m "controller: single from-config factory wired through cli and pipeline"
```

---

### Task 7: Seeder and sim fixture with S0 sim smoke

**Files:**
- Create: `tests/controllertest/seeder.py`
- Test: `tests/controllertest/test_smoke.py` (append), `tests/controllertest/test_seeder.py`

**Interfaces:**
- Produces: `SeedError(Exception)`; class `Seeder` with `__init__(self, ctl: RunningController)` and methods (all raise `SeedError` on `meta.rc != "ok"` — NEVER return empty on failure):
  - `add_site(desc: str) -> str` — returns the new site's short `name` (the URL token ubitofu targets)
  - `create_network(site: str, name: str, vlan: int, subnet: str, **extra) -> dict` (returns created object incl. `_id`)
  - `update_network(site: str, network_id: str, patch: dict) -> dict`
  - `delete_network(site: str, network_id: str) -> None`
  - `list_networks(site: str) -> list[dict]`
  - `list_devices(site: str) -> list[dict]` (uses `stat/device`)
  - `delete_device(site: str, mac: str) -> None` (`cmd/sitemgr` `delete-device`)
  - `close() -> None`
- Consumes: `readiness.login_client`, `support.RunningController`.

- [ ] **Step 1: Write the failing live tests**

`tests/controllertest/test_seeder.py`:

```python
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 James Braid
import pytest

from .seeder import Seeder, SeedError

pytestmark = pytest.mark.controller


def test_seed_network_roundtrip(seeded_controller):
    s = Seeder(seeded_controller)
    site = s.add_site("seeder-roundtrip")
    created = s.create_network(site, "seed-net", vlan=201, subnet="10.99.201.1/24")
    assert created["_id"]
    s.update_network(site, created["_id"], {"name": "seed-net-renamed"})
    names = {n["name"] for n in s.list_networks(site)}
    assert "seed-net-renamed" in names
    s.delete_network(site, created["_id"])
    s.close()


def test_seed_failure_raises_not_skips(seeded_controller):
    s = Seeder(seeded_controller)
    with pytest.raises(SeedError):
        s.create_network("nonexistent-site", "x", vlan=1, subnet="not-a-subnet")
    s.close()


def test_sim_has_demo_devices(sim_controller):
    s = Seeder(sim_controller)
    devices = s.list_devices(sim_controller.site)
    assert len(devices) >= 9, "sim contract seeds 3 APs + 1 gateway + 5 switches"
    assert all(d.get("mac") for d in devices)
    s.close()
```

Append to `tests/controllertest/test_smoke.py`:

```python
def test_sim_smoke_version(sim_controller):
    live = _live_network_version(sim_controller)
    expected = os.environ.get("UNIFI_TEST_EXPECT_VERSION")
    if expected is None and not sim_controller.external:
        expected = pins.NETWORK_VERSION
    if expected is not None:
        assert live == expected
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/controllertest/test_seeder.py -m controller -v`
Expected: ERROR — no module `seeder`.

- [ ] **Step 3: Implement seeder.py**

```python
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 James Braid
"""Thin, independent seeding client — deliberately NOT the code under test.

Contract: a failed seed fails the scenario; it must never decay into an
empty-collection skip (a fresh controller has empty collections for nearly
everything — a gate that skips on empty is vacuously green).
"""
from typing import Any

import httpx

from .readiness import login_client
from .support import RunningController


class SeedError(Exception):
    pass


class Seeder:
    def __init__(self, ctl: RunningController) -> None:
        self._client = login_client(ctl.base_url, ctl.username, ctl.password)

    def close(self) -> None:
        self._client.close()

    def _call(self, method: str, path: str, body: dict | None = None) -> list[dict]:
        resp = self._client.request(method, path, json=body)
        try:
            payload: dict[str, Any] = resp.json()
        except ValueError as exc:
            raise SeedError(f"{method} {path}: non-JSON HTTP {resp.status_code}") from exc
        if resp.status_code >= 400 or payload.get("meta", {}).get("rc") != "ok":
            raise SeedError(f"{method} {path}: HTTP {resp.status_code}: {payload.get('meta')}")
        return list(payload.get("data", []))

    # --- sites -----------------------------------------------------------
    def add_site(self, desc: str) -> str:
        data = self._call("POST", "/api/s/default/cmd/sitemgr",
                          {"cmd": "add-site", "desc": desc})
        if not data or "name" not in data[0]:
            raise SeedError(f"add-site returned no site payload: {data!r}")
        return str(data[0]["name"])

    # --- networks ---------------------------------------------------------
    def create_network(self, site: str, name: str, *, vlan: int, subnet: str,
                       **extra: object) -> dict:
        body: dict[str, object] = {
            "name": name, "purpose": "corporate",
            "vlan_enabled": True, "vlan": vlan,
            "ip_subnet": subnet, "dhcpd_enabled": False,
        }
        body.update(extra)
        data = self._call("POST", f"/api/s/{site}/rest/networkconf", body)
        if not data:
            raise SeedError("create_network: empty data")
        return data[0]

    def update_network(self, site: str, network_id: str, patch: dict) -> dict:
        data = self._call("PUT", f"/api/s/{site}/rest/networkconf/{network_id}", patch)
        if not data:
            raise SeedError("update_network: empty data")
        return data[0]

    def delete_network(self, site: str, network_id: str) -> None:
        self._call("DELETE", f"/api/s/{site}/rest/networkconf/{network_id}")

    def list_networks(self, site: str) -> list[dict]:
        return self._call("GET", f"/api/s/{site}/rest/networkconf")

    # --- devices ----------------------------------------------------------
    def list_devices(self, site: str) -> list[dict]:
        return self._call("GET", f"/api/s/{site}/stat/device")

    def delete_device(self, site: str, mac: str) -> None:
        self._call("POST", f"/api/s/{site}/cmd/sitemgr",
                   {"cmd": "delete-device", "mac": mac})
```

- [ ] **Step 4: Run live**

Run: `python -m pytest tests/controllertest/test_seeder.py tests/controllertest/test_smoke.py -m controller -v`
Expected: all pass (sim container boots here for the first time). If `create_network` is rejected with a validation error, read the `meta.msg` in the SeedError — adjust the payload minimally (e.g. some versions require `"setting_preference": "manual"`); record any addition as a comment in seeder.py with the controller version.

- [ ] **Step 5: Commit**

```bash
git add tests/controllertest/seeder.py tests/controllertest/test_seeder.py tests/controllertest/test_smoke.py
git commit -m "tests: seeding client, sim fixture coverage, mandatory-seed rule"
```

---

### Task 8: Scenario sandbox — tofu workdir, config.toml, ubitofu runner

**Files:**
- Create: `tests/controllertest/sandbox.py`
- Modify: `tests/controllertest/conftest.py` (append fixtures)
- Test: `tests/controllertest/test_sandbox.py`

**Interfaces:**
- Consumes: `RunningController` (Task 3), `Seeder` (Task 7), classic dialect (Tasks 4–6).
- Produces: class `Sandbox` with:
  - constructor `Sandbox(workdir: Path, controller: RunningController, site: str, plugin_cache: Path, monkeypatch)` — writes `providers.tf` + `config.toml`, exports `UNIFI_TEST_PASSWORD` via monkeypatch
  - `tofu(*args: str) -> subprocess.CompletedProcess[str]` — raises `RuntimeError` on nonzero exit with stderr in the message
  - `init()` — `tofu init` with the shared plugin cache
  - `apply()` — `tofu apply -auto-approve -input=false` (the deliberate, test-only exception to ubitofu's never-apply rule; the target is a disposable container)
  - `ubitofu(command: str) -> int` — runs `ubitofu.cli.main([command, "--config", ...])` in-process, returns the exit code (stdout captured by the test via capsys)
  - attribute `workdir: Path`
- Produces: conftest fixtures `plugin_cache` (session, `tmp_path_factory`-backed, sets `TF_PLUGIN_CACHE_DIR` for tofu invocations) and `make_sandbox` (function scope) → `Callable[[RunningController, str], Sandbox]`.

- [ ] **Step 1: Write the failing live test**

`tests/controllertest/test_sandbox.py`:

```python
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 James Braid
import pytest

from .seeder import Seeder

pytestmark = pytest.mark.controller


def test_sandbox_init_and_ubitofu_generate(seeded_controller, make_sandbox, capsys):
    s = Seeder(seeded_controller)
    site = s.add_site("sandbox-generate")
    s.create_network(site, "sbx-net", vlan=202, subnet="10.99.202.1/24")
    sbx = make_sandbox(seeded_controller, site)
    sbx.init()
    code = sbx.ubitofu("generate")
    out = capsys.readouterr().out
    assert code == 0, out
    generated = (sbx.workdir / "generated.tf").read_text()
    assert 'resource "unifi_network"' in generated
    assert "sbx-net" in generated or "sbx_net" in generated  # slug or name literal
    assert (sbx.workdir / "imports.tf").exists()
    s.close()
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/controllertest/test_sandbox.py -m controller -v`
Expected: ERROR — fixture `make_sandbox` not found.

- [ ] **Step 3: Implement sandbox.py**

```python
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 James Braid
"""Per-scenario tofu + ubitofu sandbox.

`apply()` is the deliberate test-only exception to ubitofu's never-apply
rule: scenarios need real state, and the target controller is a disposable
container. Production code paths under test never apply.
"""
import os
import subprocess
from pathlib import Path

from .support import RunningController

_PROVIDERS_TF = """\
terraform {{
  required_providers {{
    unifi = {{
      source = "ubiquiti-community/unifi"
    }}
  }}
}}

provider "unifi" {{
  api_url        = "{api_url}"
  username       = "{username}"
  password       = "{password}"
  site           = "{site}"
  allow_insecure = true
}}
"""

_CONFIG_TOML = """\
controller_url = "{api_url}"
site = "{site}"
dialect = "classic"
username = "{username}"
password_source = "env"
password_ref = "UNIFI_TEST_PASSWORD"
workdir = "{workdir}"
"""


class Sandbox:
    def __init__(self, workdir: Path, controller: RunningController, site: str,
                 plugin_cache: Path, monkeypatch) -> None:
        self.workdir = workdir
        self._env = {**os.environ, "TF_PLUGIN_CACHE_DIR": str(plugin_cache)}
        workdir.mkdir(parents=True, exist_ok=True)
        (workdir / "providers.tf").write_text(_PROVIDERS_TF.format(
            api_url=controller.base_url, username=controller.username,
            password=controller.password, site=site,
        ))
        self.config_path = workdir / "config.toml"
        self.config_path.write_text(_CONFIG_TOML.format(
            api_url=controller.base_url, username=controller.username,
            site=site, workdir=workdir,
        ))
        monkeypatch.setenv("UNIFI_TEST_PASSWORD", controller.password)

    def tofu(self, *args: str) -> subprocess.CompletedProcess[str]:
        proc = subprocess.run(
            ["tofu", *args], cwd=str(self.workdir), env=self._env,
            capture_output=True, text=True,
        )
        if proc.returncode != 0:
            raise RuntimeError(
                f"tofu {' '.join(args)} exited {proc.returncode}:\n{proc.stderr}"
            )
        return proc

    def init(self) -> None:
        self.tofu("init", "-input=false")

    def apply(self) -> None:
        self.tofu("apply", "-auto-approve", "-input=false")

    def ubitofu(self, command: str) -> int:
        from ubitofu.cli import main
        return main([command, "--config", str(self.config_path)])
```

Append to `tests/controllertest/conftest.py`:

```python
from pathlib import Path

from .sandbox import Sandbox


@pytest.fixture(scope="session")
def plugin_cache(tmp_path_factory) -> Path:
    cache = tmp_path_factory.mktemp("tf-plugin-cache")
    return cache


@pytest.fixture
def make_sandbox(tmp_path, plugin_cache, monkeypatch):
    def factory(controller, site: str) -> Sandbox:
        return Sandbox(tmp_path / f"wd-{site}", controller, site, plugin_cache, monkeypatch)

    return factory
```

- [ ] **Step 4: Run live**

Run: `python -m pytest tests/controllertest/test_sandbox.py -m controller -v`
Expected: PASS (~1–2 min first run: provider download + generate). If `tofu init` fails resolving `ubiquiti-community/unifi`, capture the exact error — do NOT vendor a mirror silently; surface it.

- [ ] **Step 5: Commit**

```bash
git add tests/controllertest/sandbox.py tests/controllertest/conftest.py tests/controllertest/test_sandbox.py
git commit -m "tests: per-scenario tofu sandbox and in-process ubitofu runner"
```

---

### Task 9: S1 — adopt → apply → reconcile in sync (exit 0)

**Files:**
- Create: `tests/controllertest/test_scenarios_reconcile.py`
- Create: `tests/controllertest/scenario.py`

**Interfaces:**
- Consumes: everything through Task 8.
- Produces: `scenario.adopt(seeder, controller, site, make_sandbox) -> Sandbox` helper — seed happens BEFORE calling it; it runs generate → init → apply and asserts a clean plan, returning the ready sandbox. Used by every write scenario after S1.

- [ ] **Step 1: Write the failing scenario**

`tests/controllertest/scenario.py`:

```python
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 James Braid
"""Shared write-scenario mechanics: adopt whatever the site holds."""
from .sandbox import Sandbox


def adopt(controller, site: str, make_sandbox) -> Sandbox:
    """generate → init → apply: brings the site under tofu management."""
    sbx = make_sandbox(controller, site)
    sbx.init()
    code = sbx.ubitofu("generate")
    assert code == 0, "generate must succeed before adoption"
    sbx.apply()
    return sbx
```

`tests/controllertest/test_scenarios_reconcile.py`:

```python
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 James Braid
"""Live reconcile scenarios (S1..) — spec table in
docs/superpowers/specs/2026-07-19-container-controller-testing-design.md."""
import pytest

from .scenario import adopt
from .seeder import Seeder

pytestmark = pytest.mark.controller


@pytest.fixture
def seeder(seeded_controller):
    s = Seeder(seeded_controller)
    yield s
    s.close()


def test_s1_in_sync_reconcile_exits_zero(seeded_controller, seeder, make_sandbox, capsys):
    site = seeder.add_site("s1-in-sync")
    seeder.create_network(site, "s1-net", vlan=210, subnet="10.99.210.1/24")
    sbx = adopt(seeded_controller, site, make_sandbox)
    capsys.readouterr()  # drop adoption output
    code = sbx.ubitofu("reconcile")
    out = capsys.readouterr().out
    assert code == 0, out
    assert "merged" not in out.lower() or "0" in out  # no drift captured
```

- [ ] **Step 2: Run to verify failure/finding**

Run: `python -m pytest tests/controllertest/test_scenarios_reconcile.py -m controller -v`

This is the spec's empirical question 1 (provider apply against seeded). Expected: PASS. If `apply()` fails, READ the provider error before touching anything — likely candidates are provider auth style or an attribute the seeded controller rejects. Fix by adjusting the seeded network payload or provider block; if the provider itself cannot import from a classic controller, STOP and report — that invalidates part of the spec and the human partner decides.

- [ ] **Step 3: Tighten the in-sync assertion**

Once passing, replace the loose final assert with the report's actual in-sync shape (read the captured `out` from the first green run and pin the exact section lines — e.g. absence of `merged`, `appended`, `diverged`, `orphaned` sections). The assertion must fail if any section reports items.

- [ ] **Step 4: Re-run, then commit**

Run: `python -m pytest tests/controllertest/test_scenarios_reconcile.py -m controller -v` → PASS.

```bash
git add tests/controllertest/scenario.py tests/controllertest/test_scenarios_reconcile.py
git commit -m "tests: S1 live adopt/apply/reconcile in-sync scenario"
```

---

### Task 10: S2 scalar merge + S3 depth-balance regression (exit 10)

**Files:**
- Modify: `tests/controllertest/test_scenarios_reconcile.py` (append)

**Interfaces:**
- Consumes: `adopt`, `seeder` fixture, `Sandbox` (Tasks 7–9). `EXIT_DRIFT_CAPTURED = 10` from `ubitofu.pipeline`.

- [ ] **Step 1: Write the failing tests**

```python
from ubitofu.pipeline import EXIT_ATTENTION, EXIT_DRIFT_AND_ATTENTION, EXIT_DRIFT_CAPTURED


def test_s2_scalar_drift_merges_in_place(seeded_controller, seeder, make_sandbox, capsys):
    site = seeder.add_site("s2-scalar")
    net = seeder.create_network(site, "s2-net", vlan=211, subnet="10.99.211.1/24")
    sbx = adopt(seeded_controller, site, make_sandbox)
    seeder.update_network(site, net["_id"], {"name": "s2-net-renamed"})
    capsys.readouterr()
    code = sbx.ubitofu("reconcile")
    out = capsys.readouterr().out
    assert code == EXIT_DRIFT_CAPTURED, out
    assert "merged" in out.lower()
    committed = "".join(p.read_text() for p in sbx.workdir.glob("*.tf"))
    assert "s2-net-renamed" in committed


def test_s3_scalar_drift_below_multiline_collection_merges(
        seeded_controller, seeder, make_sandbox, capsys):
    """Depth-balance regression: pre-fix, the surgeon's scanner went
    depth-negative at a multiline collection value and every later
    top-level attr became invisible — the scalar below could never merge."""
    site = seeder.add_site("s3-depth")
    net = seeder.create_network(
        site, "s3-net", vlan=212, subnet="10.99.212.1/24",
        dhcpd_enabled=True, dhcpd_dns_enabled=True,
        dhcpd_dns_1="10.99.212.53", dhcpd_dns_2="10.99.212.54",
        dhcpd_start="10.99.212.10", dhcpd_stop="10.99.212.200",
    )
    sbx = adopt(seeded_controller, site, make_sandbox)

    # Precondition for the regression: the committed block must contain a
    # multiline collection value ABOVE a scalar we are about to drift.
    # python-hcl2 renders lists multiline, and generated attrs sort with
    # dhcp_dns before name; verify rather than hope:
    committed_files = {p: p.read_text() for p in sbx.workdir.glob("*.tf")}
    block_file = next(p for p, t in committed_files.items() if "s3-net" in t)
    text = block_file.read_text()
    list_pos = text.index("[")           # first collection opener in the block file
    assert "\n" in text[list_pos:text.index("]", list_pos)], \
        "expected a multiline collection value in the committed block"
    assert text.index("name") > -1

    seeder.update_network(site, net["_id"], {"name": "s3-net-renamed"})
    capsys.readouterr()
    code = sbx.ubitofu("reconcile")
    out = capsys.readouterr().out
    assert code == EXIT_DRIFT_CAPTURED, out
    assert "s3-net-renamed" in block_file.read_text(), \
        "scalar below the multiline collection did not merge — depth-balance regression"
```

- [ ] **Step 2: Run**

Run: `python -m pytest tests/controllertest/test_scenarios_reconcile.py -m controller -v -k "s2 or s3"`
Expected: PASS against current HEAD (the fix is merged). If the S3 precondition assert fails because the generated block has no multiline list, strengthen the seed (more DNS entries) or, as a last resort, rewrite the committed list assignment into multiline form in the test before mutating — the drifted scalar must sit below a multiline collection or the test doesn't test the regression.

- [ ] **Step 3: Prove S3 catches the old bug**

Temporarily revert the fix line (`src/ubitofu/hcl_surgeon.py`: change `i = v` back to `i = eol` in `_top_level_assignments`), run S3, and confirm it FAILS (that's the regression bite). Restore the line, re-run, PASS. Do not commit the temporary revert.

- [ ] **Step 4: Commit**

```bash
git add tests/controllertest/test_scenarios_reconcile.py
git commit -m "tests: S2/S3 live scalar-merge and depth-balance scenarios"
```

---

### Task 11: S4 complex flag (11) + S5 both (12)

**Files:**
- Modify: `tests/controllertest/test_scenarios_reconcile.py` (append)

- [ ] **Step 1: Write the failing tests**

```python
def test_s4_list_drift_flags_complex(seeded_controller, seeder, make_sandbox, capsys):
    site = seeder.add_site("s4-complex")
    net = seeder.create_network(
        site, "s4-net", vlan=213, subnet="10.99.213.1/24",
        dhcpd_enabled=True, dhcpd_dns_enabled=True,
        dhcpd_dns_1="10.99.213.53",
        dhcpd_start="10.99.213.10", dhcpd_stop="10.99.213.200",
    )
    sbx = adopt(seeded_controller, site, make_sandbox)
    # List-valued drift: change the DNS set — nested/list fields are flagged,
    # never auto-edited (safety model: scalar-only).
    seeder.update_network(site, net["_id"],
                          {"dhcpd_dns_1": "10.99.213.63", "dhcpd_dns_2": "10.99.213.64"})
    capsys.readouterr()
    code = sbx.ubitofu("reconcile")
    out = capsys.readouterr().out
    assert code == EXIT_ATTENTION, out
    assert "complex" in out.lower() or "nested" in out.lower()


def test_s5_scalar_plus_list_drift_exits_12(seeded_controller, seeder, make_sandbox, capsys):
    site = seeder.add_site("s5-both")
    net = seeder.create_network(
        site, "s5-net", vlan=214, subnet="10.99.214.1/24",
        dhcpd_enabled=True, dhcpd_dns_enabled=True,
        dhcpd_dns_1="10.99.214.53",
        dhcpd_start="10.99.214.10", dhcpd_stop="10.99.214.200",
    )
    sbx = adopt(seeded_controller, site, make_sandbox)
    seeder.update_network(site, net["_id"], {
        "name": "s5-net-renamed",                     # scalar → merged (10)
        "dhcpd_dns_1": "10.99.214.63",                # list → complex (11)
    })
    capsys.readouterr()
    code = sbx.ubitofu("reconcile")
    out = capsys.readouterr().out
    assert code == EXIT_DRIFT_AND_ATTENTION, out
```

- [ ] **Step 2: Run**

Run: `python -m pytest tests/controllertest/test_scenarios_reconcile.py -m controller -v -k "s4 or s5"`
Expected: PASS. If S4 reports the DNS change as a merged scalar instead of complex, the provider models it as separate scalar attrs — switch the mutation to a genuinely nested field on the network (e.g. WAN/IPv6 sub-objects) guided by the report output; the scenario's contract is "a mutation the surgeon must flag, not merge".

- [ ] **Step 3: Commit**

```bash
git add tests/controllertest/test_scenarios_reconcile.py
git commit -m "tests: S4/S5 complex-flag and combined-outcome scenarios"
```

---

### Task 12: S6a deleted vs pending + S6b deleted device advice

**Files:**
- Modify: `tests/controllertest/test_scenarios_reconcile.py` (append S6a)
- Create: `tests/controllertest/test_scenarios_devices.py` (S6b, sim flavor)

- [ ] **Step 1: Write S6a (seeded)**

```python
def test_s6a_deleted_on_controller_vs_pending(seeded_controller, seeder, make_sandbox, capsys):
    site = seeder.add_site("s6a-diverged")
    doomed = seeder.create_network(site, "s6a-doomed", vlan=215, subnet="10.99.215.1/24")
    sbx = adopt(seeded_controller, site, make_sandbox)

    # deleted: applied resource whose live object is gone → advice must say
    # deleted (apply would NOT recreate a UI-adopted object correctly).
    seeder.delete_network(site, doomed["_id"])

    # pending: committed-but-never-applied block whose object exists live.
    live = seeder.create_network(site, "s6a-pending", vlan=216, subnet="10.99.216.1/24")
    (sbx.workdir / "pending.tf").write_text(
        'resource "unifi_network" "s6a_pending" {\n'
        '  name    = "s6a-pending"\n'
        '  purpose = "corporate"\n'
        "}\n"
    )
    capsys.readouterr()
    code = sbx.ubitofu("reconcile")
    out = capsys.readouterr().out
    assert code in (EXIT_ATTENTION, EXIT_DRIFT_AND_ATTENTION), out
    assert "deleted" in out and "s6a_doomed" in out.replace("-", "_"), out
    assert "pending" in out and "s6a_pending" in out, out
    assert live["_id"]  # keepalive for lint; the live object backs the pending branch
```

- [ ] **Step 2: Write S6b (sim, device MAC identity — no apply at all)**

`tests/controllertest/test_scenarios_devices.py`:

```python
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 James Braid
"""S6b: the deleted-DEVICE advice, live. Committed unifi_device block with a
MAC identity, no state, object removed on the controller → classify_diverged
must say deleted via the committed-values identity branch (devices carry
their MAC in config; apply cannot recreate an adopted device)."""
import pytest

from ubitofu.pipeline import EXIT_ATTENTION, EXIT_DRIFT_AND_ATTENTION

from .seeder import Seeder

pytestmark = pytest.mark.controller


def test_s6b_deleted_device_classified_deleted_not_pending(
        sim_controller, make_sandbox, capsys):
    s = Seeder(sim_controller)
    devices = s.list_devices(sim_controller.site)
    aps = [d for d in devices if d.get("type") == "uap"]
    assert aps, "sim contract seeds APs"
    victim_mac = aps[0]["mac"]

    sbx = make_sandbox(sim_controller, sim_controller.site)
    (sbx.workdir / "device.tf").write_text(
        f'resource "unifi_device" "demo_ap" {{\n  mac = "{victim_mac}"\n}}\n'
    )
    sbx.init()

    s.delete_device(sim_controller.site, victim_mac)
    capsys.readouterr()
    code = sbx.ubitofu("reconcile")
    out = capsys.readouterr().out
    s.close()
    assert code in (EXIT_ATTENTION, EXIT_DRIFT_AND_ATTENTION), out
    assert "deleted" in out and "demo_ap" in out, out
    assert "pending" not in out.split("demo_ap")[-1].splitlines()[0], \
        "device must classify deleted (committed-values MAC identity), not pending"
```

Note: this test PERMANENTLY removes a demo AP from the shared sim `default`
site — it must claim its victim by list position 0 and no other sim test may
depend on that specific device. The sim fixture is session-scoped: keep all
sim-flavor tests tolerant of "3 APs minus the ones S6b consumed" (the
`>= 9` device smoke in Task 7 runs before this in file order, but do not
rely on ordering — change the Task 7 assertion to `>= 8` NOW if this test
lands in the same session run, and note why inline).

- [ ] **Step 3: Run**

Run: `python -m pytest tests/controllertest/test_scenarios_reconcile.py -k s6a tests/controllertest/test_scenarios_devices.py -m controller -v`
Expected: PASS. S6b is the acceptance test for the reconcile-errors deleted-device advice; if the classification comes back `pending`, that is a REAL product bug or a manifest id_rule gap — investigate `classify_diverged`'s committed-values branch before touching the test.

- [ ] **Step 4: Commit**

```bash
git add tests/controllertest/test_scenarios_reconcile.py tests/controllertest/test_scenarios_devices.py tests/controllertest/test_seeder.py
git commit -m "tests: S6 deleted-vs-pending scenarios incl. live device advice"
```

---

### Task 13: S7 orphaned + S8 appended + S9 unreachable + S10 verify

**Files:**
- Modify: `tests/controllertest/test_scenarios_reconcile.py` (append S7, S8)
- Create: `tests/controllertest/test_scenarios_cli.py` (S9, S10)

- [ ] **Step 1: Write S7 + S8**

```python
def test_s7_orphaned_state_flagged(seeded_controller, seeder, make_sandbox, capsys):
    site = seeder.add_site("s7-orphan")
    seeder.create_network(site, "s7-net", vlan=217, subnet="10.99.217.1/24")
    sbx = adopt(seeded_controller, site, make_sandbox)
    # Remove the committed block while it stays in state → DESTROYED on next
    # apply; reconcile must flag it, not stay silent.
    for p in sbx.workdir.glob("*.tf"):
        if "s7-net" in p.read_text() and p.name != "providers.tf":
            content = p.read_text()
            start = content.index('resource "unifi_network"')
            end = content.index("\n}", start) + 2
            p.write_text(content[:start] + content[end:])
            break
    else:
        pytest.fail("could not find committed s7-net block to remove")
    capsys.readouterr()
    code = sbx.ubitofu("reconcile")
    out = capsys.readouterr().out
    assert code in (EXIT_ATTENTION, EXIT_DRIFT_AND_ATTENTION), out
    assert "orphan" in out.lower(), out


def test_s8_new_object_appended_with_import(seeded_controller, seeder, make_sandbox, capsys):
    site = seeder.add_site("s8-append")
    seeder.create_network(site, "s8-first", vlan=218, subnet="10.99.218.1/24")
    sbx = adopt(seeded_controller, site, make_sandbox)
    seeder.create_network(site, "s8-second", vlan=219, subnet="10.99.219.1/24")
    capsys.readouterr()
    code = sbx.ubitofu("reconcile")
    out = capsys.readouterr().out
    assert code == EXIT_DRIFT_CAPTURED, out
    appended = (sbx.workdir / "reconciled_new.tf").read_text()
    assert "s8-second" in appended or "s8_second" in appended
    assert "import {" in appended
```

- [ ] **Step 2: Write S9 + S10**

`tests/controllertest/test_scenarios_cli.py`:

```python
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 James Braid
"""S9 (no container — unreachable URL) and S10 (verify outcomes)."""
import pytest

from ubitofu.pipeline import EXIT_ATTENTION

from .scenario import adopt
from .seeder import Seeder

pytestmark = pytest.mark.controller


def test_s9_unreachable_controller_exits_1(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("UNIFI_TEST_PASSWORD", "irrelevant")
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        'controller_url = "https://127.0.0.1:1"\n'
        'site = "default"\n'
        'dialect = "classic"\n'
        'username = "admin"\n'
        'password_source = "env"\n'
        'password_ref = "UNIFI_TEST_PASSWORD"\n'
        f'workdir = "{tmp_path}"\n'
    )
    from ubitofu.cli import main
    code = main(["reconcile", "--config", str(cfg)])
    err = capsys.readouterr().err
    assert code == 1
    assert "cannot reach" in err


def test_s10_verify_clean_then_drift(seeded_controller, make_sandbox, capsys):
    s = Seeder(seeded_controller)
    site = s.add_site("s10-verify")
    net = s.create_network(site, "s10-net", vlan=220, subnet="10.99.220.1/24")
    sbx = adopt(seeded_controller, site, make_sandbox)
    capsys.readouterr()

    assert sbx.ubitofu("verify") == 0, capsys.readouterr().out  # clean plan

    s.update_network(site, net["_id"], {"name": "s10-net-drifted"})
    capsys.readouterr()
    code = sbx.ubitofu("verify")
    out = capsys.readouterr().out
    s.close()
    assert code == EXIT_ATTENTION, out  # real drift → attention required
```

Note S9 carries the `controller` marker despite needing no container: it
belongs to the scenario suite and must run wherever the suite runs; it costs
milliseconds.

- [ ] **Step 3: Run the full seeded scenario suite**

Run: `python -m pytest tests/controllertest -m "controller and not uos" -v`
Expected: everything so far passes in one session (one seeded boot, one sim boot).

- [ ] **Step 4: Commit**

```bash
git add tests/controllertest/test_scenarios_reconcile.py tests/controllertest/test_scenarios_cli.py
git commit -m "tests: S7-S10 orphaned, appended, error-path, verify scenarios"
```

---

### Task 14: UOS flavor — fixture, native-dialect bootstrap, S0 + S11 (+S12 decision)

**Files:**
- Modify: `tests/controllertest/support.py` (UOS flavor + run kwargs)
- Modify: `tests/controllertest/conftest.py` (uos fixture)
- Create: `tests/controllertest/uos.py` (native bootstrap)
- Test: `tests/controllertest/test_scenarios_uos.py`

**Interfaces:**
- Produces: `support.UOS: Flavor` (port 7443 — the direct network-app port that the image healthcheck gates on); `support.UOS_RUN_KWARGS: dict` (the documented runtime contract); fixture `uos_controller` → `RunningController` (base_url = the 7443 classic endpoint);
  `uos.native_api_key(container_host: str, https_port: int, username: str, password: str) -> str | None` — UOS SSO login + API-key mint; returns None when the mint endpoint is absent (spec decision tree #2).

- [ ] **Step 1: Extend support.py**

Append:

```python
UOS = Flavor(
    name="uos", image=pins.UOS_IMAGE,
    url_env="UNIFI_TEST_UOS_URL", image_env="UNIFI_TEST_UOS_IMAGE",
    username="admin", password="admin",
    port=7443, boot_timeout_s=600,  # image healthcheck start-period is 10 min
)

# The documented UOS runtime contract (systemd PID 1): cap list — no
# privileged mode — host cgroupns with /sys/fs/cgroup rw, tmpfs set.
# Canonical: unifi-os/examples/docker-compose.yml in unifi-containers.
UOS_RUN_KWARGS: dict = {
    "cgroupns": "host",
    "cap_drop": ["ALL"],
    "cap_add": [
        "SYS_ADMIN", "NET_ADMIN", "NET_RAW", "NET_BIND_SERVICE",
        "DAC_OVERRIDE", "DAC_READ_SEARCH", "FOWNER", "CHOWN",
        "SETUID", "SETGID", "KILL", "SYS_CHROOT", "SYS_PTRACE",
        "SYS_RESOURCE", "AUDIT_WRITE", "MKNOD",
    ],
    "tmpfs": {
        "/run": "exec", "/run/lock": "", "/tmp": "exec",
        "/var/lib/journal": "", "/var/opt/unifi/tmp": "size=64m",
    },
    "volumes": [("/sys/fs/cgroup", "/sys/fs/cgroup", "rw")],
}
```

In `boot_flavor`, container mode must also expose 443 for UOS: change the
exposed-ports line to `.with_exposed_ports(flavor.port, 443)` ONLY when
`flavor.name == "uos"` (a second port on the network flavors would change
their contract). Conftest addition:

```python
@pytest.fixture(scope="session")
def uos_controller():
    from .support import UOS, UOS_RUN_KWARGS, boot_flavor
    yield from boot_flavor(UOS, run_kwargs=UOS_RUN_KWARGS)
```

- [ ] **Step 2: Empirical probe — key mint (spec decision tree #2)**

Boot the container manually and probe before writing `uos.py`:

```bash
docker run -d --name uos-probe --cgroupns=host -v /sys/fs/cgroup:/sys/fs/cgroup:rw \
  --cap-drop ALL --cap-add SYS_ADMIN --cap-add NET_ADMIN --cap-add NET_RAW \
  --cap-add NET_BIND_SERVICE --cap-add DAC_OVERRIDE --cap-add DAC_READ_SEARCH \
  --cap-add FOWNER --cap-add CHOWN --cap-add SETUID --cap-add SETGID \
  --cap-add KILL --cap-add SYS_CHROOT --cap-add SYS_PTRACE --cap-add SYS_RESOURCE \
  --cap-add AUDIT_WRITE --cap-add MKNOD \
  --tmpfs /run:exec --tmpfs /run/lock --tmpfs /tmp:exec --tmpfs /var/lib/journal \
  --tmpfs /var/opt/unifi/tmp:size=64m \
  -p 127.0.0.1:11443:443 -p 127.0.0.1:17443:7443 \
  ghcr.io/jamesbraid/unifi-os-server:5.1.21-sim
# wait for healthy:
docker inspect -f '{{.State.Health.Status}}' uos-probe   # until "healthy"
# 1. UOS SSO login (json cookie + x-csrf-token header expected in response):
curl -ksi -X POST https://127.0.0.1:11443/api/auth/login \
  -H 'Content-Type: application/json' -d '{"username":"admin","password":"admin"}'
# 2. With the returned TOKEN cookie (+ x-csrf-token if issued), enumerate
#    self and look for an api-key resource; try in order, record results:
curl -ksi https://127.0.0.1:11443/api/users/self -b 'TOKEN=<token>'
curl -ksi -X POST https://127.0.0.1:11443/api/users/self/api-keys \
  -b 'TOKEN=<token>' -H 'x-csrf-token: <csrf>' \
  -H 'Content-Type: application/json' -d '{"name":"controllertest"}'
# 3. Whatever key comes back, prove the production dialect:
curl -ksi https://127.0.0.1:11443/proxy/network/api/s/default/rest/networkconf \
  -H 'X-API-KEY: <minted-key>'
docker rm -f uos-probe
```

Record every request/response pair in `tests/controllertest/uos.py`'s module
docstring. THEN implement `native_api_key` against what actually worked; it
returns `None` if no mint endpoint exists.

- [ ] **Step 3: Write S0-UOS + S11 (+S12 gate)**

`tests/controllertest/test_scenarios_uos.py`:

```python
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 James Braid
"""UOS-native scenarios: ubitofu's PRODUCTION dialect (/proxy/network +
X-API-KEY on 443) against a live UniFi OS Server. S11 per the spec; if the
sim image cannot mint an API key headlessly, xfail with the spec's decision:
the fallback is baking a pre-minted key into the -sim image
(unifi-containers change), NOT teaching ubitofu UOS cookie auth."""
import os

import pytest

from . import pins
from .readiness import login_client
from .uos import native_api_key

pytestmark = [pytest.mark.controller, pytest.mark.uos]


def test_s0_uos_smoke_version(uos_controller):
    # Readiness already proven by the fixture (healthcheck / login poll on
    # the 7443 network app). Version enforcement, per-flavor env:
    with login_client(uos_controller.base_url, uos_controller.username,
                      uos_controller.password) as client:
        body = client.get(f"/api/s/{uos_controller.site}/stat/sysinfo").json()
    live_network = str(body["data"][0]["version"])
    assert live_network  # the bundled network app answers with a version
    expected_uos = os.environ.get("UNIFI_TEST_UOS_EXPECT_VERSION")
    if expected_uos is None and not uos_controller.external:
        expected_uos = pins.UOS_VERSION
    # UOS platform version endpoint recorded during the Task 14 probe —
    # assert it here once known; the bundled-network-app version above is
    # the readiness half of the smoke either way.


def test_s11_native_dialect_roundtrip(uos_controller, make_sandbox, capsys, tmp_path,
                                      monkeypatch):
    key = native_api_key(uos_controller)
    if key is None:
        pytest.xfail("UOS sim cannot mint an API key headlessly — "
                     "spec decision: bake a pre-minted key into the -sim image "
                     "(unifi-containers follow-up)")
    # Native config: the exact production shape — unifi-os dialect, API key.
    monkeypatch.setenv("UNIFI_TEST_UOS_KEY", key)
    native_url = uos_controller.base_url.replace(":7443", ":443")  # container-mode ports differ; see uos.py
    workdir = tmp_path / "uos-wd"
    workdir.mkdir()
    cfg = workdir / "config.toml"
    cfg.write_text(
        f'controller_url = "{native_url}"\n'
        'site = "default"\n'
        'api_key_source = "env"\n'
        'api_key_ref = "UNIFI_TEST_UOS_KEY"\n'
        f'workdir = "{workdir}"\n'
    )
    (workdir / "providers.tf").write_text(
        'terraform {\n  required_providers {\n    unifi = {\n'
        '      source = "ubiquiti-community/unifi"\n    }\n  }\n}\n'
    )
    import subprocess
    subprocess.run(["tofu", "init", "-input=false"], cwd=workdir, check=True,
                   capture_output=True)
    from ubitofu.cli import main
    code = main(["generate", "--config", str(cfg)])
    out = capsys.readouterr().out
    assert code == 0, out
    assert (workdir / "generated.tf").exists()
```

The `native_url` construction above is a placeholder for the REAL mapping:
in container mode the 443 mapping comes from `get_exposed_port(443)`, which
the fixture must carry — extend `RunningController` with
`native_url: str = ""` populated for the UOS flavor in `boot_flavor`
(container mode: `https://{host}:{get_exposed_port(443)}`; URL mode: derive
from a `UNIFI_TEST_UOS_NATIVE_URL` env var, empty means "skip native
scenarios with `unavailable(...)`"). Implement that in this task, replace
the `.replace(":7443", ":443")` line with `uos_controller.native_url`, and
fail the test when `native_url` is empty in container mode.

- [ ] **Step 4: Run, decide S12**

Run: `python -m pytest tests/controllertest -m uos -v`
Expected: S0 passes; S11 passes or xfails per the probe result. If S11 passed AND a quick manual `tofu apply` in its workdir succeeds against UOS, add S12 (copy the S2 shape: seed a scalar change through the network-app 7443 API with `Seeder`, reconcile through the native config, assert exit 10). If apply fails, do NOT add S12 — note the failure in the test module docstring; the spec says S11 stands alone.

- [ ] **Step 5: Commit**

```bash
git add tests/controllertest/support.py tests/controllertest/conftest.py tests/controllertest/uos.py tests/controllertest/test_scenarios_uos.py
git commit -m "tests: UOS-native flavor, key-mint bootstrap, S11 dialect scenario"
```

---

### Task 15: Woodpecker controller workflow

**Files:**
- Create: `.woodpecker/` directory; Move: `.woodpecker.yml` → `.woodpecker/ci.yml` (content unchanged)
- Create: `.woodpecker/controller.yml`

**Interfaces:**
- Consumes: markers (`controller and not uos`), URL mode env names, pins (versions duplicated here as literals — keep adjacent to the image tags so they cannot drift apart silently).

- [ ] **Step 1: Move the existing workflow**

```bash
mkdir .woodpecker
git mv .woodpecker.yml .woodpecker/ci.yml
```

(Woodpecker reads all files under `.woodpecker/` once the directory exists; a root `.woodpecker.yml` would be ignored alongside it.)

- [ ] **Step 2: Write `.woodpecker/controller.yml`**

```yaml
---
# Live controller-scenario suite (spec: docs/superpowers/specs/
# 2026-07-19-container-controller-testing-design.md). The controllers run
# as Woodpecker services — plain containers, no docker socket, no DinD.
# Readiness is the harness's URL-mode login poll (rc == "ok"), because
# Woodpecker does not gate steps on service health. UOS is deliberately
# absent: its runtime contract (caps/cgroupns/tmpfs) is not expressible as
# a service under the rootless agent — UOS runs in the (dormant) GHA
# workflow and locally.

services:
  - name: seeded
    # Image tag and UNIFI_TEST_EXPECT_VERSION below MUST carry the same
    # version literal (contract: CI never runs without the expectation).
    image: ghcr.io/jamesbraid/unifi-network:10.4.57-seeded
  - name: sim
    image: ghcr.io/jamesbraid/unifi-network:10.4.57-sim

steps:
  - name: controller-tests
    image: python:3.11-bookworm
    environment:
      UNIFI_TEST_SEEDED_URL: https://seeded:8443
      UNIFI_TEST_SIM_URL: https://sim:8443
      UNIFI_TEST_EXPECT_VERSION: "10.4.57"
      UNIFI_TEST_REQUIRE: "1"
    commands:
      - bash ci/install-tofu.sh
      - pip install -e ".[dev,controller]"
      - python -m pytest tests/controllertest -m "controller and not uos" -v
    when:
      event: [push, pull_request, tag, manual]
```

- [ ] **Step 3: Validate and dry-check**

- YAML sanity: `python -c "import yaml,sys; yaml.safe_load(open('.woodpecker/controller.yml')); yaml.safe_load(open('.woodpecker/ci.yml'))"` (install pyyaml transiently with `uv run --with pyyaml` if absent).
- Local rehearsal of URL mode against real containers (proves the exact env-var path CI takes):

```bash
docker run -d --name wp-seeded -p 127.0.0.1:18443:8443 ghcr.io/jamesbraid/unifi-network:10.4.57-seeded
docker run -d --name wp-sim -p 127.0.0.1:19443:8443 ghcr.io/jamesbraid/unifi-network:10.4.57-sim
UNIFI_TEST_SEEDED_URL=https://127.0.0.1:18443 UNIFI_TEST_SIM_URL=https://127.0.0.1:19443 \
UNIFI_TEST_EXPECT_VERSION=10.4.57 UNIFI_TEST_REQUIRE=1 \
python -m pytest tests/controllertest -m "controller and not uos" -v
docker rm -f wp-seeded wp-sim
```

Expected: full pass in URL mode with EXTERNAL controllers (the `external=True` path).

- [ ] **Step 4: Commit**

```bash
git add .woodpecker/
git commit -m "ci: run controller scenarios against service containers"
```

Real-pipeline verification happens on the PR (Woodpecker picks up the new workflow); watch it with `woodpecker-cli pipeline ls infra/ubitofu` per homelab tooling — if the repo slug differs, check `woodpecker-cli repo ls`.

---

### Task 16: Dormant GHA workflow

**Files:**
- Create: `.github/workflows/controller-tests.yml`

- [ ] **Step 1: Write the workflow**

```yaml
---
# Controller-scenario suite, full matrix including UOS-native.
# DORMANT BY DESIGN: workflow_dispatch only. Enablement TODO (recorded per
# the testing contract and go-unifi precedent):
#   1. When enabling pull_request/push triggers WITH path filters AND making
#      this a required check: add an exact-inverse same-name no-op twin
#      (see go-unifi .github/workflows/integration-noop.yaml) or
#      non-matching PRs hang forever on "Expected".
#   2. Add a nightly `schedule:` run to catch upstream drift and image rot
#      independent of PR traffic.
#   3. Release process: this job becomes a gate in the release workflow
#      when releases wire through GHA (spec: out-of-scope follow-up).
# A missing image tag stays honest-red — never fall back to building.
#
# triggers (commented, dormant):
#   pull_request:
#   push: { branches: [main] }
#   schedule: [{ cron: "17 4 * * *" }]
name: controller-tests

on:
  workflow_dispatch:

jobs:
  controller-scenarios:
    runs-on: ubuntu-latest
    timeout-minutes: 45
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.11"
      - name: Install OpenTofu
        run: bash ci/install-tofu.sh
      - name: Install package with controller extras
        run: pip install -e ".[dev,controller]"
      - name: Run controller scenarios (testcontainers, incl. UOS)
        env:
          UNIFI_TEST_REQUIRE: "1"
          UNIFI_TEST_EXPECT_VERSION: "10.4.57"
          UNIFI_TEST_UOS_EXPECT_VERSION: "5.1.21"
        run: python -m pytest tests/controllertest -m controller -v
```

Check `ci/install-tofu.sh` runs on a plain ubuntu-latest runner (it was
written for the Woodpecker Debian image — read it; if it assumes root
without sudo or apt specifics, guard the GHA invocation accordingly, e.g.
`sudo bash ci/install-tofu.sh`).

- [ ] **Step 2: Validate**

- `python -c "import yaml; yaml.safe_load(open('.github/workflows/controller-tests.yml'))"`
- If `actionlint` is available (`brew install actionlint` or skip): `actionlint .github/workflows/controller-tests.yml`.
- Sanity: version literals match `tests/controllertest/pins.py` (10.4.57 / 5.1.21).

- [ ] **Step 3: Commit**

```bash
git add .github/workflows/controller-tests.yml
git commit -m "ci: dormant GHA controller-scenario workflow with enablement TODO"
```

---

### Task 17: Final verification sweep and docs touch

**Files:**
- Modify: `README.md` (self-hosted dialect note), `CHANGELOG.md`

- [ ] **Step 1: Full matrix locally**

```bash
python -m pytest                       # default suite — no containers, green
mypy src && ruff check .               # clean
python -m pytest tests/controllertest -m "controller and not uos" -v   # classic flavors
python -m pytest tests/controllertest -m uos -v                        # UOS flavor
```

All green (S11 may be a recorded xfail per Task 14's probe outcome).

- [ ] **Step 2: Docs**

- README: under the config documentation, add the classic-dialect keys with a
  4-line example (dialect/username/password_source/password_ref) and one
  sentence: self-hosted standalone controllers use `dialect = "classic"`;
  UniFi OS consoles keep the default.
- CHANGELOG (Unreleased): entries for classic dialect support and the
  live controller-scenario suite (marker `controller`, contract env vars).

- [ ] **Step 3: Commit**

```bash
git add README.md CHANGELOG.md
git commit -m "docs: classic dialect config and live scenario suite notes"
```

---

## Self-Review (performed while writing)

- **Spec coverage:** S0→Task 3/7/14; S1→9; S2/S3→10; S4/S5→11; S6a/S6b→12; S7–S10→13; S11/S12→14; classic dialect→4–6; contract env/readiness/seed rules→2/3/7; Woodpecker→15; dormant GHA→16; version enforcement→3/7/14/15/16; skip-vs-fail→3.
- **Known empirical gates (not placeholders — spec decision trees):** Task 9 step 2 (provider apply), Task 14 step 2 (key mint probe) with recorded fallbacks and stop conditions.
- **Type consistency:** `RunningController(base_url, username, password, site, external)` used by Tasks 3/7/8/12/14 (Task 14 extends it with `native_url`); `Seeder(ctl)` methods as declared in Task 7's interface block; exit-code constants imported from `ubitofu.pipeline` everywhere.
