# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 James Braid
import re
import subprocess

import pytest

from ubitofu.cleaner import VarRef
from ubitofu.errors import TofuExecutionError
from ubitofu.hcl_writer import render_resource, tofu_fmt


def test_render_resource_is_a_pure_in_memory_renderer(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("resource rendering must not execute OpenTofu")

    monkeypatch.setattr(subprocess, "run", forbidden)

    rendered = render_resource("unifi_network", "lan", {"name": "lan"})

    assert rendered == 'resource "unifi_network" "lan" {\n  name = "lan"\n}\n'


def test_owned_hcl_formatter_is_pure_and_deterministic(monkeypatch):
    from ubitofu.hcl_writer import format_owned_hcl

    def forbidden(*args, **kwargs):
        raise AssertionError("owned formatting must not execute OpenTofu")

    monkeypatch.setattr(subprocess, "run", forbidden)
    source = 'resource "x" "y" {\n  short     = 1  \n  longer = 2\n}\n'

    assert format_owned_hcl(source) == (
        'resource "x" "y" {\n  short  = 1\n  longer = 2\n}\n'
    )


def test_tofu_fmt_failure_uses_the_safe_typed_error(monkeypatch) -> None:
    def fail(*args, **kwargs):
        return subprocess.CompletedProcess(
            args=["tofu", "fmt", "-"],
            returncode=1,
            stdout="",
            stderr="provider stderr api_key=synthetic-secret",
        )

    monkeypatch.setattr(subprocess, "run", fail)

    with pytest.raises(TofuExecutionError) as exc_info:
        tofu_fmt("not valid hcl")

    assert exc_info.value.command == "fmt"
    assert exc_info.value.exit_code == 1
    assert exc_info.value.reason == "execution failed"
    assert "synthetic-secret" not in str(exc_info.value)


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
    # External formatting is an adapter concern; the renderer itself is pure.
    assert tofu_fmt(hcl)


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
            {"port_idx": 2, "name": "example_device"}]},
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


def test_render_variables_declarations():
    from ubitofu.hcl_writer import render_variables

    out = render_variables(["wlan_examplenet_psk", "dynamic_dns_home_password"])
    # sorted, one declaration each, sensitive string vars
    assert out.index('variable "dynamic_dns_home_password"') < \
        out.index('variable "wlan_examplenet_psk"')
    assert out.count("variable ") == 2
    assert out.count("type      = string") == 2
    assert out.count("sensitive = true") == 2
    assert tofu_fmt(out) == out  # already fmt-clean


def test_render_variables_empty():
    from ubitofu.hcl_writer import render_variables

    assert render_variables([]) == ""


def test_render_variable_is_the_single_declaration_block() -> None:
    """Catches incremental rendering regenerating unrelated variable declarations."""
    from ubitofu.hcl_writer import render_variable

    assert render_variable("wlan_guest_psk") == (
        'variable "wlan_guest_psk" {\n  type      = string\n  sensitive = true\n}\n'
    )


def test_scalar_shapes_render_as_hcl_literals():
    hcl = render_resource(
        "t", "a", {"s": "x", "b": True, "nb": False, "n": 3, "f": 1.5, "z": None}
    )

    assert hcl == (
        'resource "t" "a" {\n'
        '  s  = "x"\n'
        "  b  = true\n"
        "  nb = false\n"
        "  n  = 3\n"
        "  f  = 1.5\n"
        "  z  = null\n"
        "}\n"
    )


def test_lists_render_one_item_per_line_and_empty_collections_inline():
    hcl = render_resource("t", "a", {"l": ["a", "b"], "el": [], "eo": {}})

    assert hcl == (
        'resource "t" "a" {\n'
        "  l  = [\n"
        '    "a",\n'
        '    "b",\n'
        "  ]\n"
        "  el = []\n"
        "  eo = {}\n"
        "}\n"
    )


def test_nested_objects_and_lists_of_objects_render_recursively():
    hcl = render_resource(
        "t", "a", {"o": {"k": "v", "il": [1, 2]}, "lo": [{"a": 1}, {"b": "two"}]}
    )

    assert hcl == (
        'resource "t" "a" {\n'
        "  o  = {\n"
        '    k  = "v",\n'
        "    il = [\n"
        "      1,\n"
        "      2,\n"
        "    ],\n"
        "  }\n"
        "  lo = [\n"
        "    {\n"
        "      a = 1,\n"
        "    },\n"
        "    {\n"
        '      b = "two",\n'
        "    },\n"
        "  ]\n"
        "}\n"
    )


def test_repeated_blocks_are_separated_by_single_blank_lines():
    hcl = render_resource(
        "t", "b",
        {"name": "n", "po": [{"idx": 1, "nm": "u"}, {"idx": 2}]},
        block_attrs=("po",),
    )

    assert hcl == (
        'resource "t" "b" {\n'
        '  name = "n"\n'
        "\n"
        "  po {\n"
        "    idx = 1\n"
        '    nm  = "u"\n'
        "  }\n"
        "\n"
        "  po {\n"
        "    idx = 2\n"
        "  }\n"
        "}\n"
    )


def test_lifecycle_follows_attributes_and_blocks_after_one_blank_line():
    hcl = render_resource(
        "t", "g",
        {"name": "n", "po": [{"idx": 1}]},
        lifecycle={"ignore_changes": ["x", "y"]},
        block_attrs=("po",),
    )

    assert hcl == (
        'resource "t" "g" {\n'
        '  name = "n"\n'
        "\n"
        "  po {\n"
        "    idx = 1\n"
        "  }\n"
        "\n"
        "  lifecycle {\n"
        "    ignore_changes = [x, y]\n"
        "  }\n"
        "}\n"
    )


def test_empty_resource_renders_a_closed_block():
    assert render_resource("t", "f", {}) == 'resource "t" "f" {\n}\n'
    assert render_resource("t", "d", {"po": []}, block_attrs=("po",)) == (
        'resource "t" "d" {\n}\n'
    )


def test_writer_has_no_python_hcl2_dependency():
    """Catches the HCL emitter depending on a parser library at runtime."""
    import importlib.metadata

    import ubitofu.hcl_writer as writer

    runtime = [
        requirement
        for requirement in importlib.metadata.requires("ubitofu") or []
        if "extra ==" not in requirement
    ]
    assert not any(requirement.lower().startswith("python-hcl2") for requirement in runtime)
    assert not hasattr(writer, "hcl2")
    assert not hasattr(writer, "Builder")
