# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 James Braid
"""A herded device fleet, adopted by this harness.

The scenarios that exercise the whole boundary: our controller on our own
network, the herder starting devices on it with no credentials and no
knowledge of adoption, and this side driving each reported MAC to connected
and then into generated HCL.

Needs a real Docker daemon (marker `herder`), so it is excluded wherever the
runner has no socket — see .woodpecker/controller.yml.
"""
import pytest

from .adopt import adopt_fleet, devices_by_mac
from .readiness import login_client

pytestmark = [pytest.mark.controller, pytest.mark.herder]


def test_herded_devices_adopt_and_reach_connected(sim_fleet):
    controller, devices = sim_fleet
    assert devices, "the herder reported ready with no devices"

    adopt_fleet(controller, [d.mac for d in devices])

    # Re-read independently of the driver's own polling: the assertion is
    # what the controller holds, not what the driver believed.
    with login_client(controller.base_url, controller.username, controller.password) as client:
        held = devices_by_mac(client, controller.site)
    for device in devices:
        doc = held.get(device.mac.lower())
        assert doc is not None, f"{device.mac} ({device.model}) is not on the controller"
        assert (doc.get("state"), bool(doc.get("adopted"))) == (1, True), (
            f"{device.mac} ({device.model}) is state={doc.get('state')!r} "
            f"adopted={doc.get('adopted')!r}"
        )
        assert doc.get("model") == device.model, (
            f"{device.mac} adopted as {doc.get('model')!r}, herder reported {device.model!r}"
        )


def test_herded_devices_reach_the_fail_closed_generation_snapshot(
    adopted_sim_fleet, make_sandbox
):
    # The sim has known coverage gaps, so 0.10 must not expose a committable
    # candidate. The adopted devices still have to cross the whole controller,
    # import, provider-plan, and provider-shaped snapshot boundary. Rendering
    # their pure fragments proves the exact MACs would enter HCL once coverage
    # is complete without weakening that gate here.
    controller, devices = adopted_sim_fleet

    sbx = make_sandbox(controller, controller.site)
    sbx.init()
    preview = sbx.generation_preview()

    assert preview.blocked is True
    assert preview.candidates == ()
    assert not (sbx.workdir / "generated.tf").exists()
    resources = {
        dict(resource.values.items).get("mac"): resource
        for resource in preview.snapshot.resources
        if resource.resource_type == "unifi_device"
    }
    schema = dict(preview.snapshot.schema.resources)["unifi_device"]
    from ubitofu.generate import _render_generated_resource
    for device in devices:
        resource = resources.get(device.mac)
        assert resource is not None, f"{device.model} {device.mac} is missing from the snapshot"
        rendered, _ = _render_generated_resource(resource, schema)
        assert device.mac.encode() in rendered


def test_ready_identities_are_unique_per_device(sim_fleet):
    # Devices batched into one synthetic container intentionally SHARE an
    # ip, so identity is the MAC — never the address.
    _, devices = sim_fleet
    assert len({d.mac for d in devices}) == len(devices)
    assert len({d.serial for d in devices}) == len(devices)
    assert len({d.name for d in devices}) == len(devices)
