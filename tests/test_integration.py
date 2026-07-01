import json
import re

from unifi_tofu_import.enumerator import ImportTarget
from unifi_tofu_import.pipeline import build_hcl, new_targets, state_identities


def test_build_hcl_from_planned_values_matches_golden(fixtures_dir):
    schema = json.loads((fixtures_dir / "schema.json").read_text())
    planned = json.loads((fixtures_dir / "show_planned.json").read_text())
    hcl = build_hcl(planned, schema)            # slug = res["name"] (M4)
    expected = (fixtures_dir / "golden" / "e2e_expected.tf").read_text()
    assert hcl == expected


def test_build_hcl_drops_empty_and_renders_block_types(fixtures_dir):
    schema = json.loads((fixtures_dir / "schema.json").read_text())
    planned = json.loads((fixtures_dir / "show_planned.json").read_text())
    hcl = build_hcl(planned, schema)
    assert 'domain_name = ""' not in hcl   # empty dropped
    assert "\n  id " not in hcl            # computed-only dropped
    assert "dhcp_server = {" in hcl        # nested-object attribute preserved
    # C2: block_types wired end-to-end — port_override renders as a block,
    # not an attribute assignment, and the empty second name is dropped.
    assert "port_override {" in hcl
    assert "port_override = [" not in hcl
    assert re.search(r'name\s*=\s*"uplink"', hcl)  # tofu fmt may pad spacing


def test_incremental_skips_already_managed(fixtures_dir):
    # State fixture already manages net001 (by id) and one client (by MAC).
    class FakeRunner:
        def show_state_json(self):
            return json.loads((fixtures_dir / "state.json").read_text())

    managed = state_identities(FakeRunner())
    targets = [
        ImportTarget("unifi_network", "examplenet", "net001"),    # managed -> skip
        ImportTarget("unifi_network", "newnet", "net999"),      # NEW -> keep
        ImportTarget("unifi_client", "server", "aa:bb:cc:00:00:01"),  # managed(MAC)->skip
        ImportTarget("unifi_client", "laptop", "aa:bb:cc:00:00:02"),  # NEW(MAC)->keep
    ]
    fresh = new_targets(targets, managed)
    assert {t.import_id for t in fresh} == {"net999", "aa:bb:cc:00:00:02"}


def test_incremental_mac_identity_matching():
    # A MAC-keyed type must match state on the `mac` attribute, NOT `id`:
    # the state row's id is the controller _id, but the import identity is MAC.
    class FakeRunner:
        def show_state_json(self):
            return {"values": {"root_module": {"resources": [
                {"type": "unifi_client", "name": "server",
                 "values": {"id": "u1", "mac": "aa:bb:cc:00:00:01"}}]}}}

    managed = state_identities(FakeRunner())
    assert managed["unifi_client"] == {"aa:bb:cc:00:00:01"}   # keyed by MAC
    assert "u1" not in managed["unifi_client"]                # NOT the _id
