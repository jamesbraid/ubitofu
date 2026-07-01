import re

from unifi_tofu_import.cleaner import VarRef
from unifi_tofu_import.hcl_writer import render_resource, tofu_fmt


def test_nested_object_vs_list_of_object_PIN():
    # THE RISK PIN: dhcp_server is a nested OBJECT ({...}); radio_table is a
    # LIST-of-object ([{...}]). Both must render distinctly and validly.
    # (Synthetic attrs — this exercises the WRITER, not schema validity;
    # dhcp_server/radio_table live on different real resources.)
    hcl = render_resource("unifi_network", "examplenet", {
        "name": "examplenet",
        "enabled": True,
        "dhcp_server": {"enabled": True, "start": "10.0.0.10"},
        "radio_table": [
            {"name": "ra0", "ht": 20},
            {"name": "rai0", "ht": 80},
        ],
        "servers": ["192.0.2.254"],
    })
    # nested object: "dhcp_server = {" with no leading "["
    assert "dhcp_server = {" in hcl
    assert "dhcp_server = [" not in hcl
    # list-of-object: "radio_table = [" then object entries
    assert "radio_table = [" in hcl
    # inner list-of-object names render quoted; assert with fmt-tolerant regex
    # (tofu fmt aligns "=" with variable padding, so match `name<ws>=<ws>"..."`)
    assert re.search(r'name\s*=\s*"ra0"', hcl)
    assert re.search(r'name\s*=\s*"rai0"', hcl)
    # the outer resource name is the top-level "examplenet" scalar
    assert re.search(r'name\s*=\s*"examplenet"', hcl)
    assert "enabled = true" in hcl
    # tofu fmt round-trip is idempotent (output already formatted)
    assert tofu_fmt(hcl) == hcl


def test_varref_renders_unquoted():
    hcl = render_resource("unifi_wlan", "examplenet",
                          {"name": "examplenet", "passphrase": VarRef("var.wlan_examplenet_psk")})
    assert "passphrase = var.wlan_examplenet_psk" in hcl
    assert '"var.wlan_examplenet_psk"' not in hcl  # NOT quoted


def test_lifecycle_block_rendered():
    hcl = render_resource("unifi_wlan", "examplenet",
                          {"name": "examplenet"},
                          lifecycle={"ignore_changes": ["passphrase_wo"]})
    assert "lifecycle {" in hcl
    assert "ignore_changes = [passphrase_wo]" in hcl  # attr refs, unquoted inside []


def test_repeated_nested_block():
    # port_override is a real block_type on unifi_device (nesting_mode=set).
    # Given entries, each renders as a distinct `port_override { … }` block —
    # NOT a `port_override = [ … ]` attribute assignment.
    hcl = render_resource(
        "unifi_device", "switch1",
        {"name": "switch1", "port_override": [
            {"port_idx": 1, "name": "uplink"},
            {"port_idx": 2, "name": "cam"}]},
        block_attrs=("port_override",)).strip()
    assert hcl.startswith('resource "unifi_device" "switch1"')
    assert hcl.count("port_override {") == 2          # two blocks, not one list
    assert "port_override = [" not in hcl             # NOT an attribute
    assert re.search(r'port_idx\s*=\s*1', hcl)


def test_string_escaping_is_safe():
    # Controller data can contain quotes, backslashes and "${...}" — none may
    # break out of the string or be interpreted as an HCL interpolation.
    hcl = render_resource("unifi_network", "n1",
                          {"name": r'a"b\c ${x} %{y}'})
    assert r'\"' in hcl                      # quote escaped
    assert r"\\" in hcl                      # backslash escaped
    assert "$${x}" in hcl and "%%{y}" in hcl  # interpolation neutralised
    assert tofu_fmt(hcl) == hcl              # still valid, fmt-idempotent


def test_string_escaping_control_chars():
    # UniFi note/description fields can contain newlines, tabs, and carriage
    # returns.  A literal newline inside an HCL string literal makes tofu fmt
    # raise "Invalid multi-line string" — so _q must escape them.
    hcl = render_resource("unifi_network", "n2",
                          {"name": "line1\nline2\ttab\rend"})
    # The literal characters must not appear in the emitted HCL.
    assert "\n" not in hcl.split("line1")[1].split('"')[0] + "x"  # rough guard
    # The escape sequences must be present as two-character sequences.
    assert r"\n" in hcl
    assert r"\t" in hcl
    assert r"\r" in hcl
    # tofu fmt must not crash (would raise RuntimeError on multi-line string).
    assert tofu_fmt(hcl) == hcl


def test_matches_golden_network(fixtures_dir):
    hcl = render_resource("unifi_network", "examplenet", {
        "name": "examplenet", "enabled": True,
        "dhcp_server": {"enabled": True, "start": "10.0.0.10"},
    })
    expected = (fixtures_dir / "golden" / "network_nested.tf").read_text()
    assert hcl == expected
