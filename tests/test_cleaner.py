# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 James Braid
from ubitofu.cleaner import VarRef, clean_resource, is_empty, is_settable

SCHEMA = {"block": {"attributes": {
    "name":     {"type": "string", "required": True},
    "enabled":  {"type": "bool",   "optional": True},
    "id":       {"type": "string", "computed": True},          # computed-only -> skip
    "subnet":   {"type": "string", "optional": True, "computed": True},  # settable
    "domain_name": {"type": "string", "optional": True},
    "passphrase":  {"type": "string", "optional": True, "sensitive": True},
}}}


def test_is_settable():
    attrs = SCHEMA["block"]["attributes"]
    assert is_settable(attrs["name"]) is True
    assert is_settable(attrs["enabled"]) is True
    assert is_settable(attrs["subnet"]) is True       # optional+computed still settable
    assert is_settable(attrs["id"]) is False          # computed-only


def test_is_empty():
    assert is_empty(None) and is_empty("") and is_empty([]) and is_empty({})
    assert not is_empty(False) and not is_empty(0)    # real values kept


def test_clean_drops_computed_and_empties_keeps_false():
    values = {"name": "lan", "enabled": False, "id": "abc",
              "subnet": "10.0.0.0/24", "domain_name": ""}
    out = clean_resource(values, SCHEMA)
    assert out == {"name": "lan", "enabled": False, "subnet": "10.0.0.0/24"}
    assert "id" not in out            # computed-only dropped
    assert "domain_name" not in out   # empty string dropped (invalid input)


def test_sensitive_becomes_varref_even_when_null():
    values = {"name": "wifi", "passphrase": None}
    out = clean_resource(values, SCHEMA,
                         sensitive={"passphrase": VarRef("var.wlan_examplenet_psk")})
    assert out["passphrase"] == VarRef("var.wlan_examplenet_psk")


# Schema shape mirrors `unifi_device`: a `port_override` repeated block lives
# under block["block_types"], NOT block["attributes"].
BLOCK_SCHEMA = {"block": {
    "attributes": {"name": {"type": "string", "optional": True}},
    "block_types": {
        "port_override": {"nesting_mode": "set", "block": {"attributes": {
            "port_idx": {"type": "number", "optional": True},
            "name":     {"type": "string", "optional": True},
            "op_mode":  {"type": "string", "computed": True},  # computed-only -> skip
        }}},
    },
}}


def test_block_types_are_cleaned_not_dropped():
    values = {"name": "switch1", "port_override": [
        {"port_idx": 1, "name": "uplink", "op_mode": "switch"},
        {"port_idx": 2, "name": "", "op_mode": "switch"},  # empty name dropped
    ]}
    out = clean_resource(values, BLOCK_SCHEMA)
    # the block survives (was previously dropped) and is a list of entries
    assert out["port_override"] == [{"port_idx": 1, "name": "uplink"},
                                    {"port_idx": 2}]
    assert "op_mode" not in out["port_override"][0]   # computed-only skipped


def test_empty_block_types_omitted():
    out = clean_resource({"name": "switch1", "port_override": []}, BLOCK_SCHEMA)
    assert "port_override" not in out


# Schema shape mirrors `unifi_firewall_policy.destination`: a single-nested
# OBJECT attribute (schema `nested_type`, nesting_mode "single") — NOT a
# block_type. The provider read returns null/empty children plus a
# computed-only child; both must be cleaned, not emitted raw.
NESTED_ATTR_SCHEMA = {"block": {"attributes": {
    "name": {"type": "string", "optional": True},
    "destination": {"required": True, "nested_type": {
        "nesting_mode": "single",
        "attributes": {
            "matching_target":      {"type": "string", "required": True},
            "zone_id":              {"type": "string", "required": True},
            "matching_target_type": {"type": "string", "computed": True},
            "client_macs":          {"type": "list", "optional": True, "computed": True},
            "ip_group_id":          {"type": "string", "optional": True, "computed": True},
        },
    }},
}}}


def test_nested_type_attribute_is_cleaned_not_emitted_raw():
    values = {"name": "policy1", "destination": {
        "matching_target": "ANY",
        "zone_id": "zone-abc",
        "matching_target_type": "SPECIFIC",  # computed-only -> must drop
        "client_macs": None,                 # empty -> drop
        "ip_group_id": "",                   # empty -> drop
    }}
    out = clean_resource(values, NESTED_ATTR_SCHEMA)
    assert out["destination"] == {"matching_target": "ANY", "zone_id": "zone-abc"}


def test_empty_nested_type_attribute_omitted():
    values = {"name": "policy1", "destination": {
        "matching_target": "", "zone_id": "",
        "matching_target_type": "SPECIFIC", "client_macs": None,
    }}
    out = clean_resource(values, NESTED_ATTR_SCHEMA)
    assert "destination" not in out  # wholly-empty after cleaning -> dropped


def test_normalize_port_forward_wan_interface_all_to_both():
    from ubitofu.cleaner import normalize_emitted

    attrs = {"name": "svc1", "wan": {"interface": "all", "port": "443"}}
    out = normalize_emitted("unifi_port_forward", attrs)
    # Provider rejects "all"; "both" is the 2-WAN equivalent.
    assert out["wan"]["interface"] == "both"
    assert out["wan"]["port"] == "443"


def test_normalize_leaves_valid_wan_interface_untouched():
    from ubitofu.cleaner import normalize_emitted

    attrs = {"name": "ssh", "wan": {"interface": "wan2"}}
    assert normalize_emitted("unifi_port_forward", attrs)["wan"]["interface"] == "wan2"


def test_normalize_ignores_other_resource_types():
    from ubitofu.cleaner import normalize_emitted

    attrs = {"name": "x", "wan": {"interface": "all"}}
    # Only unifi_port_forward.wan.interface is normalized.
    assert normalize_emitted("unifi_network", attrs)["wan"]["interface"] == "all"


# ---------------------------------------------------------------------------
# Value-pattern secret safety net (the WireGuard lesson): the live provider
# returned WG server private keys in PLAINTEXT with no schema `sensitive`
# flag. After cleaning, secret-shaped values must be stripped generically.
# ---------------------------------------------------------------------------

WG_KEY = "a" * 43 + "="   # curve25519/WireGuard key shape: 44-char base64, '='


def test_strip_secret_shaped_by_attr_name():
    from ubitofu.cleaner import strip_secret_shaped

    attrs = {"name": "wg", "x_passphrase": "plaintext-psk", "port": 51820}
    hits = strip_secret_shaped(attrs)
    assert hits == ["x_passphrase"]
    assert attrs == {"name": "wg", "port": 51820}


def test_strip_secret_shaped_by_wg_key_shape_regardless_of_name():
    from ubitofu.cleaner import strip_secret_shaped

    attrs = {"name": "wg", "tunnel_material": WG_KEY}
    hits = strip_secret_shaped(attrs)
    assert hits == ["tunnel_material"]
    assert "tunnel_material" not in attrs


def test_strip_secret_shaped_public_key_exempt():
    from ubitofu.cleaner import strip_secret_shaped

    # WG PUBLIC keys share the shape but are not secrets (and may be required
    # attrs, e.g. unifi_wireguard_peer.public_key) — never strip them.
    attrs = {"name": "peer", "public_key": WG_KEY}
    assert strip_secret_shaped(attrs) == []
    assert attrs["public_key"] == WG_KEY


def test_strip_secret_shaped_skips_varrefs():
    from ubitofu.cleaner import strip_secret_shaped

    attrs = {"passphrase": VarRef("var.wlan_examplenet_psk")}
    assert strip_secret_shaped(attrs) == []
    assert attrs["passphrase"] == VarRef("var.wlan_examplenet_psk")


def test_strip_secret_shaped_recurses_nested_and_lists():
    from ubitofu.cleaner import strip_secret_shaped

    attrs = {
        "wireguard": {"private_key": WG_KEY, "port": 51820},
        "peers": [{"name": "a", "preshared_secret": "hunter2"},
                  {"name": "b"}],
    }
    hits = strip_secret_shaped(attrs)
    assert hits == ["wireguard.private_key", "peers[0].preshared_secret"]
    assert attrs["wireguard"] == {"port": 51820}
    assert attrs["peers"] == [{"name": "a"}, {"name": "b"}]


def test_strip_secret_shaped_drops_emptied_containers():
    from ubitofu.cleaner import strip_secret_shaped

    attrs = {"wireguard": {"private_key": WG_KEY}}
    hits = strip_secret_shaped(attrs)
    assert hits == ["wireguard.private_key"]
    assert attrs == {}  # emptied dict removed entirely


def test_strip_secret_shaped_bool_and_int_names_untouched():
    from ubitofu.cleaner import strip_secret_shaped

    # Name-pattern rule applies to STRING values only — password_enabled etc.
    attrs = {"password_enabled": True, "token_ttl": 3600}
    assert strip_secret_shaped(attrs) == []
    assert attrs == {"password_enabled": True, "token_ttl": 3600}
