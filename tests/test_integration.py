# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 James Braid
import json
import re

from ubitofu.enumerator import ImportTarget
from ubitofu.pipeline import build_hcl, new_targets, state_identities


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
        ImportTarget("unifi_client", "client_b", "00:11:22:00:00:01"),  # managed(MAC)->skip
        ImportTarget("unifi_client", "client_a", "00:11:22:00:00:02"),  # NEW(MAC)->keep
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
            "name": "ddns_example_net",
            "values": {
                "host_name": "ddns.example.net",
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
            "name": "alt_ddns_example_net",
            "values": {
                "host_name": "alt-ddns.example.net",
                "service":   "dyndns",
                "login":     "examplenet",
                # password is sensitive; provider returns null for sensitive attrs
                "password":  None,
            },
        }]}}
    }
    hcl = build_hcl(planned, schema)
    # password must be assigned as a var ref, not suppressed
    assert "var.dynamic_dns_alt_ddns_example_net_password" in hcl
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
                {"type": "unifi_client", "name": "client_b",
                 "values": {"id": "cid1", "mac": "00:11:22:00:00:01"}}]}}}

    managed = state_identities(FakeRunner())
    assert managed["unifi_client"] == {"00:11:22:00:00:01"}   # keyed by MAC
    assert "cid1" not in managed["unifi_client"]                # NOT the _id


# ---------------------------------------------------------------------------
# Value-pattern secret safety net, end-to-end through build():
# the live provider returned WG server private keys in PLAINTEXT with no
# schema sensitive flag — the pipeline must catch secret-shaped values.
# ---------------------------------------------------------------------------

WG_KEY = "a" * 43 + "="


def _one_resource_plan(rtype, slug, attrs_schema, values):
    schema = {"provider_schemas": {
        "registry.opentofu.org/ubiquiti-community/unifi": {
            "resource_schemas": {rtype: {"block": {"attributes": attrs_schema}}}}}}
    planned = {"planned_values": {"root_module": {"resources": [
        {"type": rtype, "name": slug, "values": values}]}}}
    return planned, schema


def test_build_suppresses_secret_shaped_plaintext_and_warns():
    from ubitofu.pipeline import build

    planned, schema = _one_resource_plan(
        "unifi_network", "wg",
        {
            "name":         {"type": "string", "required": True},
            # NOT flagged sensitive in the schema — exactly the WG lesson.
            "x_passphrase": {"type": "string", "optional": True},
            "private_key":  {"type": "string", "optional": True},
        },
        {"name": "wg", "x_passphrase": "plaintext-psk", "private_key": WG_KEY},
    )
    result = build(planned, schema)
    assert "plaintext-psk" not in result.hcl
    assert WG_KEY not in result.hcl
    assert "x_passphrase =" not in result.hcl
    assert "private_key =" not in result.hcl
    # both suppressed attrs covered by lifecycle ignore_changes
    assert "ignore_changes" in result.hcl
    assert "x_passphrase" in result.hcl
    assert "private_key" in result.hcl
    assert result.secret_warnings == [
        "unifi_network.wg: private_key", "unifi_network.wg: x_passphrase"]


def test_build_wg_shape_in_nested_attr_suppressed():
    from ubitofu.pipeline import build

    planned, schema = _one_resource_plan(
        "unifi_network", "wg",
        {
            "name": {"type": "string", "required": True},
            "wireguard": {"optional": True, "nested_type": {
                "nesting_mode": "single",
                "attributes": {
                    "tunnel_material": {"type": "string", "optional": True},
                    "port":            {"type": "number", "optional": True},
                }}},
        },
        {"name": "wg", "wireguard": {"tunnel_material": WG_KEY, "port": 51820}},
    )
    result = build(planned, schema)
    assert WG_KEY not in result.hcl
    assert "port = 51820" in result.hcl
    # top-level attr goes into ignore_changes; warning names the full path
    assert "ignore_changes = [wireguard]" in result.hcl
    assert result.secret_warnings == ["unifi_network.wg: wireguard.tunnel_material"]


def test_build_public_key_shaped_value_kept():
    from ubitofu.pipeline import build

    planned, schema = _one_resource_plan(
        "unifi_wireguard_peer", "peer_a",
        {
            "name":       {"type": "string", "required": True},
            "public_key": {"type": "string", "required": True},
        },
        {"name": "peer_a", "public_key": WG_KEY},
    )
    result = build(planned, schema)
    assert WG_KEY in result.hcl          # public keys are not secrets
    assert result.secret_warnings == []


def test_build_hcl_still_returns_plain_string(fixtures_dir):
    import json

    from ubitofu.pipeline import build, build_hcl
    schema = json.loads((fixtures_dir / "schema.json").read_text())
    planned = json.loads((fixtures_dir / "show_planned.json").read_text())
    assert build_hcl(planned, schema) == build(planned, schema).hcl


# ---------------------------------------------------------------------------
# run_verify: exit-2 plans pass iff the diff is secrets-only (schema-derived
# sensitive map); anything else fails with the drift itemized.
# ---------------------------------------------------------------------------

class _VerifyRunner:
    def __init__(self, exit_code, plan_json, schema=None):
        self._code = exit_code
        self._plan = plan_json
        self._schema = schema or {"provider_schemas": {}}

    def plan(self, *, out=None, generate_config_out=None):
        return self._code

    def show_json(self, plan_file):
        return self._plan

    def providers_schema(self):
        return self._schema

    def is_clean(self, code):
        return code == 0


_WLAN_SCHEMA = {"provider_schemas": {
    "registry.opentofu.org/ubiquiti-community/unifi": {"resource_schemas": {
        "unifi_wlan": {"block": {"attributes": {
            "name":       {"type": "string", "required": True},
            "passphrase": {"type": "string", "optional": True, "sensitive": True},
        }}}}}}}


def _run_verify_with(runner, monkeypatch, tmp_path):
    import io

    import ubitofu.pipeline as pl
    from ubitofu.config import Config
    monkeypatch.setattr(pl, "TofuRunner", lambda workdir: runner)
    cfg = Config("https://unifi.example", "default", "env", "UNIFI_API_KEY",
                 "ExampleVault", workdir=str(tmp_path))
    out = io.StringIO()
    return pl.run_verify(cfg, out), out.getvalue()


def test_verify_clean_plan_passes(monkeypatch, tmp_path):
    runner = _VerifyRunner(0, {"resource_changes": []})
    rc, output = _run_verify_with(runner, monkeypatch, tmp_path)
    assert rc == 0
    assert "0 changes" in output


def test_verify_secrets_only_diff_passes(monkeypatch, tmp_path):
    plan = {"resource_changes": [{
        "type": "unifi_wlan", "address": "unifi_wlan.examplenet",
        "change": {"actions": ["update"],
                   "before": {"name": "examplenet", "passphrase": "a"},
                   "after":  {"name": "examplenet", "passphrase": "b"}}}]}
    runner = _VerifyRunner(2, plan, _WLAN_SCHEMA)
    rc, output = _run_verify_with(runner, monkeypatch, tmp_path)
    assert rc == 0
    assert "secrets-only" in output


def test_verify_real_drift_fails_and_itemizes(monkeypatch, tmp_path):
    plan = {"resource_changes": [{
        "type": "unifi_wlan", "address": "unifi_wlan.examplenet",
        "change": {"actions": ["update"],
                   "before": {"name": "examplenet", "passphrase": "a"},
                   "after":  {"name": "renamed", "passphrase": "a"}}}]}
    runner = _VerifyRunner(2, plan, _WLAN_SCHEMA)
    rc, output = _run_verify_with(runner, monkeypatch, tmp_path)
    assert rc == 11  # attention required: real drift
    assert "unifi_wlan.examplenet" in output
    assert "update" in output


def test_block_attrs_derived_from_schema_block_types():
    """Repeated blocks render as blocks for ANY type, driven by schema block_types."""
    from ubitofu.pipeline import build

    # A type the old hardcoded BLOCK_ATTRS map never knew about.
    schema = {"provider_schemas": {
        "registry.opentofu.org/ubiquiti-community/unifi": {"resource_schemas": {
            "unifi_fake_blocky": {"block": {
                "attributes": {"name": {"type": "string", "required": True}},
                "block_types": {"entry": {"nesting_mode": "set", "block": {
                    "attributes": {
                        "idx":   {"type": "number", "optional": True},
                        "label": {"type": "string", "optional": True},
                    }}}},
            }}}}}}
    planned = {"planned_values": {"root_module": {"resources": [{
        "type": "unifi_fake_blocky", "name": "b1",
        "values": {"name": "b1", "entry": [{"idx": 1, "label": "x"},
                                           {"idx": 2, "label": "y"}]},
    }]}}}
    hcl = build(planned, schema).hcl
    assert hcl.count("entry {") == 2      # blocks, not an attribute
    assert "entry = [" not in hcl


# ---------------------------------------------------------------------------
# variables.tf emission: generate output must be self-contained — every
# var.<name> referenced by the emitted HCL gets a sensitive declaration.
# ---------------------------------------------------------------------------

def test_build_collects_var_names_and_op_refs():
    from ubitofu.pipeline import build

    planned, schema = _one_resource_plan(
        "unifi_wlan", "examplenet",
        {
            "name":       {"type": "string", "required": True},
            "passphrase": {"type": "string", "optional": True, "sensitive": True},
        },
        {"name": "examplenet", "passphrase": None},
    )
    result = build(planned, schema, vault="ExampleVault")
    assert result.var_names == ["wlan_examplenet_psk"]
    assert result.op_refs == {
        "wlan_examplenet_psk": "op://ExampleVault/unifi.wifi-psk.examplenet/password"
    }


def test_write_variables_tf_bulk_overwrites(tmp_path):
    from ubitofu.pipeline import write_variables_tf

    vf = tmp_path / "unifi-variables.tf"
    vf.write_text('variable "stale_var" {\n  type      = string\n'
                  '  sensitive = true\n}\n')
    write_variables_tf(tmp_path, ["wlan_examplenet_psk"], merge=False)
    text = vf.read_text()
    assert 'variable "wlan_examplenet_psk"' in text
    assert "stale_var" not in text  # bulk regenerates the whole set


def test_write_variables_tf_incremental_merges(tmp_path):
    from ubitofu.pipeline import write_variables_tf

    vf = tmp_path / "unifi-variables.tf"
    vf.write_text('variable "wlan_examplenet_psk" {\n  type      = string\n'
                  '  sensitive = true\n}\n')
    write_variables_tf(tmp_path, ["dynamic_dns_home_password"], merge=True)
    text = vf.read_text()
    # existing declarations kept, new one added, no duplicates
    assert text.count('variable "wlan_examplenet_psk"') == 1
    assert text.count('variable "dynamic_dns_home_password"') == 1


def test_run_generate_emits_variables_and_prints_op_refs(monkeypatch, tmp_path, fixtures_dir):
    import io

    import ubitofu.pipeline as pl
    from ubitofu.config import Config
    from ubitofu.enumerator import EnumerationResult, ImportTarget

    # Coverage fixture schema: has unifi_setting (audit needs it) plus enough
    # shape for _one_resource_plan's unifi_wlan resource to still build/emit.
    coverage_schema = json.loads(
        (fixtures_dir / "coverage" / "providers_schema.json").read_text())
    planned, wlan_schema = _one_resource_plan(
        "unifi_wlan", "examplenet",
        {
            "name":       {"type": "string", "required": True},
            "passphrase": {"type": "string", "optional": True, "sensitive": True},
        },
        {"name": "examplenet", "passphrase": None},
    )
    schema = {"provider_schemas": {
        **coverage_schema["provider_schemas"],
        **wlan_schema["provider_schemas"],
    }}

    class FakeRunner:
        def __init__(self, workdir):
            pass

        def plan(self, *, out=None, generate_config_out=None):
            if generate_config_out is not None:
                generate_config_out.write_text("# stub\n")
            return 0

        def providers_schema(self):
            return schema

        def show_json(self, plan_file):
            return planned

    class FakeController:
        site = "default"

        def collection(self, endpoint):
            return []  # get/setting + every PROBE_ENDPOINTS entry: no gaps

    monkeypatch.setattr(pl, "TofuRunner", FakeRunner)
    monkeypatch.setattr(pl, "Controller", lambda **kw: FakeController())
    monkeypatch.setattr(pl, "enumerate_controller", lambda ctl: EnumerationResult(
        targets=[ImportTarget("unifi_wlan", "examplenet", "wlan001")], gaps=[]))
    monkeypatch.setenv("UNIFI_API_KEY", "k")

    cfg = Config("https://unifi.example", "default", "env", "UNIFI_API_KEY",
                 "ExampleVault", workdir=str(tmp_path))
    out = io.StringIO()
    rc = pl.run_generate(cfg, "bulk", out)
    assert rc == 0
    vars_tf = (tmp_path / "unifi-variables.tf").read_text()
    assert 'variable "wlan_examplenet_psk"' in vars_tf
    assert "sensitive = true" in vars_tf
    assert "op://ExampleVault/unifi.wifi-psk.examplenet/password" in out.getvalue()
    assert "var.wlan_examplenet_psk" in (tmp_path / "generated.tf").read_text()

    coverage_md = tmp_path / "COVERAGE.md"
    assert coverage_md.exists()
    assert coverage_md.read_text().startswith("# Provider coverage")
