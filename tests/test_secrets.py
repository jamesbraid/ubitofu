# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 James Braid
from ubitofu.cleaner import VarRef
from ubitofu.secrets import (
    SecretRule,
    op_reference,
    resolve_secrets,
    sensitive_attrs,
    var_name,
)

SCHEMA = {"block": {"attributes": {
    "name":       {"type": "string", "required": True},
    "passphrase": {"type": "string", "optional": True, "sensitive": True},
}}}


def test_sensitive_attrs_detected_from_schema():
    assert sensitive_attrs(SCHEMA) == {"passphrase"}


def test_var_name_templated():
    rule = SecretRule("unifi_wlan", "passphrase", "wlan_{name}_psk",
                      "op://{vault}/unifi.wifi-psk.{name}/password")
    assert var_name(rule, {"name": "examplenet"}) == "wlan_examplenet_psk"


def test_op_reference_uses_config_vault():
    rule = SecretRule("unifi_wlan", "passphrase", "wlan_{name}_psk",
                      "op://{vault}/unifi.wifi-psk.{name}/password")
    assert op_reference(rule, {"name": "examplenet"}, vault="ExampleVault") == \
        "op://ExampleVault/unifi.wifi-psk.examplenet/password"


def test_resolve_secrets_returns_varref_and_lifecycle():
    refs, lifecycle, suppress = resolve_secrets("unifi_wlan", "examplenet", SCHEMA)
    assert refs == {"passphrase": VarRef("var.wlan_examplenet_psk")}
    assert lifecycle == {"ignore_changes": ["passphrase_wo"]}
    assert suppress == set()  # all sensitive attrs are covered by a rule


def test_sensitive_attrs_detects_write_only():
    schema = {"block": {"attributes": {
        "name": {"type": "string", "required": True},
        "secret": {"type": "string", "optional": True, "write_only": True},
    }}}
    assert sensitive_attrs(schema) == {"secret"}


def test_resolve_secrets_no_matching_rule_suppresses_sensitive():
    # unifi_network has no SECRETS rule; the schema's passphrase is sensitive
    # and unsourced → suppress it and add it to ignore_changes.
    refs, lifecycle, suppress = resolve_secrets("unifi_network", "lan", SCHEMA)
    assert refs == {}
    assert suppress == {"passphrase"}
    assert lifecycle == {"ignore_changes": ["passphrase"]}


def test_var_name_slugifies_hyphens():
    # slug may come from a resource name like "my-wifi" → context name = "my-wifi"
    # var_template uses {name} directly; caller is responsible for normalising
    rule = SecretRule("unifi_wlan", "passphrase", "wlan_{name}_psk",
                      "op://{vault}/unifi.wifi-psk.{name}/password")
    assert var_name(rule, {"name": "my-wifi"}) == "wlan_my-wifi_psk"


def test_op_reference_no_homelab_hardcoded():
    """vault comes from the caller — no homelab values in source."""
    rule = SecretRule("unifi_wlan", "passphrase", "wlan_{name}_psk",
                      "op://{vault}/unifi.wifi-psk.{name}/password")
    ref = op_reference(rule, {"name": "test"}, vault="MyVault")
    assert "ExampleVault" not in ref
    assert ref == "op://MyVault/unifi.wifi-psk.test/password"


# ---------------------------------------------------------------------------
# New tests: sensitive attrs with/without SECRETS rules → suppress / merge
# ---------------------------------------------------------------------------

# Schema for a hypothetical resource with two sensitive attrs:
# - "password" has NO SECRETS rule (unsourced)
# - "token" will be given a SECRETS rule in some tests
_DDNS_LIKE_SCHEMA = {"block": {"attributes": {
    "host_name": {"type": "string", "required": True},
    "service":   {"type": "string", "required": True},
    "password":  {"type": "string", "optional": True, "sensitive": True},
    "token":     {"type": "string", "optional": True, "sensitive": True},
}}}


def test_unsourced_sensitive_attr_in_suppress_and_ignore_changes():
    """(a) Sensitive attr with no SECRETS rule → in suppress AND ignore_changes."""
    # Neither "password" nor "token" has a SECRETS rule for "unifi_fake_ddns"
    refs, lifecycle, suppress = resolve_secrets(
        "unifi_fake_ddns", "home", _DDNS_LIKE_SCHEMA
    )
    assert refs == {}
    # Both unsourced sensitive attrs → suppressed
    assert suppress == {"password", "token"}
    # Both appear in ignore_changes (sorted, so deterministic)
    assert lifecycle == {"ignore_changes": ["password", "token"]}


def test_sourced_sensitive_attr_still_varref():
    """(b) Sensitive attr WITH a SECRETS rule → var. ref, NOT in suppress."""
    # passphrase on unifi_wlan IS in SECRETS → var.ref, suppress is empty
    refs, lifecycle, suppress = resolve_secrets("unifi_wlan", "examplenet", SCHEMA)
    assert refs == {"passphrase": VarRef("var.wlan_examplenet_psk")}
    assert "passphrase" not in suppress


def test_dynamic_dns_password_resolves_to_varref():
    """unifi_dynamic_dns.password has a SECRETS rule → VarRef, not suppressed."""
    schema = {"block": {"attributes": {
        "host_name": {"type": "string", "required": True},
        "service":   {"type": "string", "required": True},
        "password":  {"type": "string", "optional": True, "sensitive": True},
    }}}
    refs, lifecycle, suppress = resolve_secrets(
        "unifi_dynamic_dns", "example_home_example_net", schema
    )
    assert refs == {
        "password": VarRef("var.dynamic_dns_example_home_example_net_password")
    }
    assert "password" not in suppress
    # No lifecycle_ignore on this rule and no unsourced attrs → empty lifecycle
    assert lifecycle == {}


def test_dynamic_dns_password_not_in_ignore_changes():
    """Confirm password is absent from ignore_changes when the rule fires."""
    schema = {"block": {"attributes": {
        "host_name": {"type": "string", "required": True},
        "service":   {"type": "string", "required": True},
        "password":  {"type": "string", "optional": True, "sensitive": True},
    }}}
    _, lifecycle, suppress = resolve_secrets(
        "unifi_dynamic_dns", "example_home_example_net", schema
    )
    ignore = lifecycle.get("ignore_changes", [])
    assert "password" not in ignore
    assert suppress == set()


def test_dynamic_dns_op_reference_uses_config_vault():
    """op_template for dynamic_dns is fully generic: {vault} + {name}, no fixed item."""
    rule = next(r for r in __import__(
        "ubitofu.secrets", fromlist=["SECRETS"]).SECRETS
        if r.resource_type == "unifi_dynamic_dns")
    from ubitofu.secrets import op_reference
    ref = op_reference(rule, {"name": "example_home_example_net"}, vault="ExampleVault")
    assert ref == "op://ExampleVault/dynamic-dns.example_home_example_net/password"
    # Vault is config-driven — nothing environment-specific in the template itself
    ref2 = op_reference(rule, {"name": "example_home_example_net"}, vault="OtherVault")
    assert ref2 == "op://OtherVault/dynamic-dns.example_home_example_net/password"


def test_mixed_sourced_and_unsourced_merges_ignore_changes():
    """(c) Schema with both: sourced attr (lifecycle_ignore) + unsourced → merged."""
    # Build a schema where passphrase (sourced via SECRETS for unifi_wlan) and
    # an additional unsourced sensitive attr "password" coexist.
    mixed_schema = {"block": {"attributes": {
        "name":       {"type": "string", "required": True},
        "passphrase": {"type": "string", "optional": True, "sensitive": True},
        "password":   {"type": "string", "optional": True, "sensitive": True},
    }}}
    refs, lifecycle, suppress = resolve_secrets("unifi_wlan", "home", mixed_schema)
    # passphrase is sourced → VarRef, not suppressed
    assert refs == {"passphrase": VarRef("var.wlan_home_psk")}
    assert "passphrase" not in suppress
    # password is unsourced → suppressed
    assert suppress == {"password"}
    # ignore_changes contains BOTH the rule's lifecycle_ignore AND the unsourced attr
    assert set(lifecycle["ignore_changes"]) == {"passphrase_wo", "password"}


def test_secret_sources_maps_var_names_to_op_refs():
    from ubitofu.secrets import secret_sources

    sources = secret_sources("unifi_wlan", "examplenet", SCHEMA, vault="ExampleVault")
    assert sources == {
        "wlan_examplenet_psk": "op://ExampleVault/unifi.wifi-psk.examplenet/password"
    }


def test_secret_sources_empty_when_no_rule_matches():
    from ubitofu.secrets import secret_sources

    assert secret_sources("unifi_network", "lan", SCHEMA, vault="ExampleVault") == {}
