# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 James Braid
"""Flavor definitions and container/URL-mode boot for controller fixtures.

Skip-vs-fail per the contract: missing docker or URL env is a friendly
skip locally; with UNIFI_TEST_REQUIRE set (CI always sets it) the same
condition is a hard failure — no skip may satisfy a required check.
"""
import os
import sys
import warnings
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
    scheme: str = "https"  # base_url scheme for `port`, in container mode


@dataclass(frozen=True)
class RunningController:
    base_url: str
    username: str
    password: str
    site: str
    external: bool  # True in URL mode — never assert pin-derived facts then
    # UOS-only: the unifi-os dialect endpoint (443, /proxy/network +
    # X-API-KEY). Empty for non-UOS flavors and whenever URL mode has no
    # UNIFI_TEST_UOS_NATIVE_URL — callers must treat empty as unavailable.
    native_url: str = ""


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
UOS = Flavor(
    name="uos", image=pins.UOS_IMAGE,
    url_env="UNIFI_TEST_UOS_URL", image_env="UNIFI_TEST_UOS_IMAGE",
    username="admin", password="admin",
    port=7443, boot_timeout_s=600,  # image healthcheck start-period is 10 min
    # 7443 is systemd-socket-proxyd fronting the bundled Network App's own
    # 127.0.0.1:8081 — plain HTTP by the image's own entrypoint contract
    # (UOS_NETWORK_DIRECT="Direct (SSO-free) UniFi Network API port").
    # Confirmed empirically during the Task 14 probe: an HTTPS handshake
    # against the mapped port hangs/fails ([SSL: WRONG_VERSION_NUMBER]);
    # plain HTTP gets a clean {"meta":{"rc":"ok"}}. 443 (native_url) is
    # unaffected — that's real nginx-terminated TLS.
    scheme="http",
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


def _report_keep(flavor: Flavor, base_url: str) -> None:
    """Surface UNIFI_TEST_KEEP's "container left running" notice.

    warnings.warn (not print) so it lands in pytest's warnings summary
    unconditionally — a bare print() is swallowed by pytest's output
    capture unless the run passes -s, so the operator would set
    UNIFI_TEST_KEEP and see no confirmation the container was actually
    kept.
    """
    warnings.warn(
        f"UNIFI_TEST_KEEP set — leaving {flavor.name} container running: {base_url}",
        stacklevel=2,
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


def _ensure_vm_socket_override() -> None:
    # testcontainers' Ryuk reaper bind-mounts the client-visible docker
    # socket path into its own container. On macOS every engine is
    # VM-based (colima, Docker Desktop) and that host path does not exist
    # inside the VM — Ryuk dies with "error while creating mount source
    # path". The VM-side socket is /var/run/docker.sock; point
    # testcontainers at it. Preserves the reaper backstop — never disable
    # Ryuk here. Respect an explicit operator override.
    if sys.platform != "darwin" or os.environ.get("TESTCONTAINERS_DOCKER_SOCKET_OVERRIDE"):
        return
    os.environ["TESTCONTAINERS_DOCKER_SOCKET_OVERRIDE"] = "/var/run/docker.sock"


def boot_flavor(flavor: Flavor, run_kwargs: dict | None = None) -> Iterator[RunningController]:
    is_uos = flavor.name == "uos"
    url = os.environ.get(flavor.url_env)
    if url:
        base_url = url.rstrip("/")
        wait_ready(base_url, flavor.username, flavor.password,
                   timeout_s=flavor.boot_timeout_s)
        native_url = ""
        if is_uos:
            # No docker container to read a mapped port from in URL mode —
            # the operator must supply the native (443, unifi-os dialect)
            # endpoint directly. Unset means "skip native scenarios".
            native_url = os.environ.get("UNIFI_TEST_UOS_NATIVE_URL", "").rstrip("/")
        yield RunningController(base_url, flavor.username, flavor.password,
                                site="default", external=True, native_url=native_url)
        return

    if not _docker_available():
        unavailable(f"docker unavailable and {flavor.url_env} unset")

    _ensure_vm_socket_override()

    from testcontainers.core.container import DockerContainer
    from testcontainers.core.wait_strategies import HealthcheckWaitStrategy

    image = os.environ.get(flavor.image_env, flavor.image)
    container = DockerContainer(image, **(run_kwargs or {}))
    # UOS alone also exposes 443 — the unifi-os dialect (native) endpoint,
    # distinct from the 7443 bundled-network-app port the healthcheck and
    # base_url use. A second port on the other flavors would change their
    # documented contract, so this is UOS-only.
    container = container.with_exposed_ports(flavor.port, 443) if is_uos \
        else container.with_exposed_ports(flavor.port)
    container = container.waiting_for(
        HealthcheckWaitStrategy().with_startup_timeout(int(flavor.boot_timeout_s))
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

    host = container.get_container_host_ip()
    base_url = f"{flavor.scheme}://{host}:{container.get_exposed_port(flavor.port)}"
    native_url = f"https://{host}:{container.get_exposed_port(443)}" if is_uos else ""
    try:
        yield RunningController(base_url, flavor.username, flavor.password,
                                site="default", external=False, native_url=native_url)
    finally:
        if os.environ.get("UNIFI_TEST_KEEP"):
            _report_keep(flavor, base_url)
        else:
            container.stop()
