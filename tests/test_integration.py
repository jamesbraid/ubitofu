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
        ImportTarget("unifi_client", "server", "00:11:22:00:00:01"),  # managed(MAC)->skip
        ImportTarget("unifi_client", "laptop", "00:11:22:00:00:02"),  # NEW(MAC)->keep
    ]
    fresh = new_targets(targets, managed)
    assert {t.import_id for t in fresh} == {"net999", "00:11:22:00:00:02"}


def test_unsourced_sensitive_omitted_and_lifecycle_added():
    """build_hcl: sensitive attr with no SECRETS rule → not an assignment + ignore_changes."""
    schema = {
        "provider_schemas": {
            "registry.opentofu.org/ubiquiti-community/unifi": {
                "resource_schemas": {
                    "unifi_fake_service": {"block": {"attributes": {
                        "host_name": {"type": "string", "required": True},
                        "service":   {"type": "string", "required": True},
                        "login":     {"type": "string", "optional": True},
                        "password":  {"type": "string", "optional": True,
                                      "sensitive": True},
                    }}}
                }
            }
        }
    }
    planned = {
        "planned_values": {"root_module": {"resources": [{
            "type": "unifi_fake_service",
            "name": "home_example_net",
            "values": {
                "host_name": "home.example.net",
                "service":   "dyndns",
                "login":     "myuser",
                # password is sensitive; provider returns null for sensitive attrs
                "password":  None,
            },
        }]}}
    }
    hcl = build_hcl(planned, schema)
    # The sensitive attr must NOT appear as an assignment (no plaintext, no null assign)
    assert "password =" not in hcl
    # A lifecycle block with ignore_changes for the sensitive attr must be present
    assert "lifecycle" in hcl
    assert "ignore_changes" in hcl
    assert "password" in hcl   # attr name appears inside ignore_changes = [password]


def test_dynamic_dns_password_sourced_via_secrets_rule():
    """build_hcl: unifi_dynamic_dns.password has a SECRETS rule → var ref, no suppress."""
    schema = {
        "provider_schemas": {
            "registry.opentofu.org/ubiquiti-community/unifi": {
                "resource_schemas": {
                    "unifi_dynamic_dns": {"block": {"attributes": {
                        "host_name": {"type": "string", "required": True},
                        "service":   {"type": "string", "required": True},
                        "login":     {"type": "string", "optional": True},
                        "password":  {"type": "string", "optional": True,
                                      "sensitive": True},
                    }}}
                }
            }
        }
    }
    planned = {
        "planned_values": {"root_module": {"resources": [{
            "type": "unifi_dynamic_dns",
            "name": "example_home_example_net",
            "values": {
                "host_name": "example-home.example.net",
                "service":   "dyndns",
                "login":     "examplenet",
                # password is sensitive; provider returns null for sensitive attrs
                "password":  None,
            },
        }]}}
    }
    hcl = build_hcl(planned, schema)
    # password must be assigned as a var ref, not suppressed
    assert "var.dynamic_dns_example_home_example_net_password" in hcl
    # NOT a plaintext assignment or null
    assert 'password = null' not in hcl
    # No ignore_changes for password (it has a rule, not suppressed)
    assert "ignore_changes" not in hcl


def test_unsourced_sensitive_with_nonull_value_also_omitted():
    """Sensitive attr value from controller must not leak as plaintext even if non-null."""
    schema = {
        "provider_schemas": {
            "registry.opentofu.org/ubiquiti-community/unifi": {
                "resource_schemas": {
                    "unifi_fake": {"block": {"attributes": {
                        "name":   {"type": "string", "required": True},
                        "secret": {"type": "string", "optional": True,
                                   "sensitive": True},
                    }}}
                }
            }
        }
    }
    planned = {
        "planned_values": {"root_module": {"resources": [{
            "type": "unifi_fake",
            "name": "res1",
            "values": {"name": "myres", "secret": "s3cr3t-value"},
        }]}}
    }
    hcl = build_hcl(planned, schema)
    assert "s3cr3t-value" not in hcl   # plaintext must never appear
    assert "secret =" not in hcl       # must not be emitted as an assignment
    assert "lifecycle" in hcl          # lifecycle block must be present
    assert "ignore_changes" in hcl
    assert "secret" in hcl             # attr name appears inside ignore_changes = [secret]


def test_incremental_mac_identity_matching():
    # A MAC-keyed type must match state on the `mac` attribute, NOT `id`:
    # the state row's id is the controller _id, but the import identity is MAC.
    class FakeRunner:
        def show_state_json(self):
            return {"values": {"root_module": {"resources": [
                {"type": "unifi_client", "name": "server",
                 "values": {"id": "u1", "mac": "00:11:22:00:00:01"}}]}}}

    managed = state_identities(FakeRunner())
    assert managed["unifi_client"] == {"00:11:22:00:00:01"}   # keyed by MAC
    assert "u1" not in managed["unifi_client"]                # NOT the _id
