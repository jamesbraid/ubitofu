# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 James Braid
"""Flavor definitions and container/URL-mode boot for controller fixtures.

Skip-vs-fail per the contract: missing docker or URL env is a friendly
skip locally; with UNIFI_TEST_REQUIRE set (CI always sets it) the same
condition is a hard failure — no skip may satisfy a required check.
"""
import os
import sys
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

    _ensure_vm_socket_override()

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
