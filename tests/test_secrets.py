from unifi_tofu_import.cleaner import VarRef
from unifi_tofu_import.secrets import (
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
    refs, lifecycle = resolve_secrets("unifi_wlan", "examplenet", SCHEMA)
    assert refs == {"passphrase": VarRef("var.wlan_examplenet_psk")}
    assert lifecycle == {"ignore_changes": ["passphrase_wo"]}


def test_sensitive_attrs_detects_write_only():
    schema = {"block": {"attributes": {
        "name": {"type": "string", "required": True},
        "secret": {"type": "string", "optional": True, "write_only": True},
    }}}
    assert sensitive_attrs(schema) == {"secret"}


def test_resolve_secrets_no_match_returns_empty():
    refs, lifecycle = resolve_secrets("unifi_network", "lan", SCHEMA)
    assert refs == {}
    assert lifecycle == {}


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
