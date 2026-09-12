# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 James Braid
"""Emit the HCL ubitofu owns: resource blocks and sensitive variable stubs.

The emitter is deliberately small. Values come from the provider schema
already cleaned, so it only has to write scalars, objects, lists, repeated
nested blocks, variable references, and a lifecycle block, in one fixed
style: two-space indents, one item per line, `=` aligned within a block, and
a trailing comma after every object or list item.
"""

import json
import re
import subprocess

from .cleaner import VarRef
from .errors import TofuExecutionError

_ASSIGNMENT_LINE = re.compile(
    r"^(?P<indent>\s*)(?P<name>[A-Za-z_][A-Za-z0-9_-]*)\s*=\s*(?P<value>.*)$"
)


def _q(s: str) -> str:
    """Quote a string literal for HCL.

    Internal special characters are escaped:
      - backslash first (avoid double-escaping later additions)
      - double-quote
      - HCL interpolation opener ${…} → $${…}
      - HCL template opener %{…} → %%{…}
    """
    s = (s.replace("\\", "\\\\")
           .replace('"', '\\"')
           .replace("\n", "\\n")
           .replace("\r", "\\r")
           .replace("\t", "\\t")
           .replace("${", "$${")
           .replace("%{", "%%{"))
    return f'"{s}"'


def _render_value(value: object, indent: int) -> str:
    """Render one attribute value; a multi-line value closes at ``indent``."""
    pad = "  " * indent
    if isinstance(value, VarRef):
        return value.expr
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "null"
    if isinstance(value, str):
        return _q(value)
    if isinstance(value, dict):
        if not value:
            return "{}"
        entries = _assignment_lines(value, indent + 1, trailing=",")
        return "{\n" + "".join(entries) + pad + "}"
    if isinstance(value, list | tuple):
        if not value:
            return "[]"
        inner = "  " * (indent + 1)
        items = "".join(f"{inner}{_render_value(item, indent + 1)},\n" for item in value)
        return "[\n" + items + pad + "]"
    return str(value)  # int / float


def _assignment_lines(attrs: dict[str, object], indent: int, trailing: str = "") -> list[str]:
    """Render ``name = value`` lines with ``=`` aligned across the group."""
    if not attrs:
        return []
    pad = "  " * indent
    width = max(len(name) for name in attrs)
    return [
        f"{pad}{name.ljust(width)} = {_render_value(value, indent)}{trailing}\n"
        for name, value in attrs.items()
    ]


def _block(name: str, entry: dict[str, object], indent: int) -> str:
    """Render one repeated nested block, preceded by a blank line."""
    pad = "  " * indent
    return "\n" + f"{pad}{name} {{\n" + "".join(_assignment_lines(entry, indent + 1)) + f"{pad}}}\n"


def tofu_fmt(text: str, binary: str = "tofu") -> str:
    """Run `tofu fmt -` on *text* and return the formatted result."""
    proc = subprocess.run(
        [binary, "fmt", "-"],
        input=text,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise TofuExecutionError("fmt", proc.returncode, "execution failed")
    return proc.stdout


def _render_lifecycle_raw(lifecycle: dict[str, object]) -> str:
    """Render a lifecycle block.

    ``ignore_changes`` holds attribute references, not strings, and stays on
    one line: tofu fmt keeps single-line lists intact.
    """
    lines = ["  lifecycle {"]
    for k, v in lifecycle.items():
        if isinstance(v, list):
            refs = ", ".join(str(r) for r in v)
            lines.append(f"    {k} = [{refs}]")
    lines.append("  }")
    return "\n".join(lines)


def format_owned_hcl(text: str) -> str:
    """Apply the deterministic subset of tofu fmt used by owned blocks in memory."""
    compact: list[str] = []
    for raw in text.splitlines():
        line = raw.rstrip()
        if not line:
            compact.append("")
            continue
        compact.append(line)
    while compact and not compact[-1]:
        compact.pop()

    formatted: list[str] = []
    index = 0
    while index < len(compact):
        match = _ASSIGNMENT_LINE.match(compact[index])
        if match is None or match.group("value").lstrip().startswith(("{", "[", "<<")):
            formatted.append(compact[index])
            index += 1
            continue
        group = [match]
        cursor = index + 1
        while cursor < len(compact):
            following = _ASSIGNMENT_LINE.match(compact[cursor])
            if (
                following is None
                or following.group("indent") != match.group("indent")
                or following.group("value").lstrip().startswith(("{", "[", "<<"))
            ):
                break
            group.append(following)
            cursor += 1
        width = max(len(item.group("name")) for item in group)
        formatted.extend(
            f'{item.group("indent")}{item.group("name").ljust(width)} = {item.group("value")}'
            for item in group
        )
        index = cursor
    return "\n".join(formatted) + "\n"


def render_resource(
    resource_type: str,
    slug: str,
    attrs: dict[str, object],
    lifecycle: dict[str, object] | None = None,
    block_attrs: tuple[str, ...] = (),
) -> str:
    """Render a single Terraform/OpenTofu resource block to HCL.

    Constructs handled:
    - Scalar / string attributes (string literals quoted via ``_q``).
    - Nested object attribute: dict value → ``name = { … }`` HCL object.
    - List-of-object attribute: list-of-dict → ``name = [ { … }, … ]``.
    - Repeated nested blocks (``block_attrs``): each list entry → a separate
      ``name { … }`` block rather than an ``= […]`` assignment.
    - VarRef: rendered as a bare ``var.<name>`` traversal expression.
    - lifecycle: ``ignore_changes`` refs stay unquoted on one line.
    """
    scalar_attrs = {k: v for k, v in attrs.items() if k not in block_attrs}
    body = "".join(_assignment_lines(scalar_attrs, 1))
    for name in block_attrs:
        entries = attrs.get(name)
        if isinstance(entries, list):
            body += "".join(_block(name, entry, 1) for entry in entries if isinstance(entry, dict))
    if lifecycle:
        body += "\n" + _render_lifecycle_raw(lifecycle) + "\n"
    return f"resource {_q(resource_type)} {_q(slug)} {{\n{body}}}\n"


def render_variables(var_names: list[str]) -> str:
    """Render sensitive string variable declarations, sorted by name.

    Only declarations — values come from the operator's secret manager
    (TF_VAR_* / -var-file); no secret value is ever written to a file.
    """
    blocks = [
        f'variable "{name}" {{\n  type      = string\n  sensitive = true\n}}\n'
        for name in sorted(set(var_names))
    ]
    return "\n".join(blocks)


def render_variable(name: str) -> str:
    """Render one sensitive string variable without touching other declarations."""
    return render_variables([name])


def render_json_fallback(
    resource_type: str,
    slug: str,
    attrs: dict[str, object],
) -> str:
    """Emit a ``.tf.json`` resource block as a fallback.

    Used when native HCL cannot express a construct the provider schema
    requires. VarRef values become ``${var.name}`` interpolation expressions,
    which are valid in .tf.json.
    """
    def _plain(v: object) -> object:
        if isinstance(v, VarRef):
            return f"${{{v.expr}}}"
        if isinstance(v, dict):
            return {k: _plain(x) for k, x in v.items()}
        if isinstance(v, list):
            return [_plain(x) for x in v]
        return v

    payload = {
        "resource": {
            resource_type: {
                slug: {k: _plain(v) for k, v in attrs.items()}
            }
        }
    }
    return json.dumps(payload, indent=2) + "\n"
