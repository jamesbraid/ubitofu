from unifi_tofu_import.cleaner import VarRef, clean_resource, is_empty, is_settable

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
