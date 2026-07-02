import json
import subprocess

import hcl2  # type: ignore[import-untyped]
from hcl2 import Builder

from .cleaner import VarRef


def _q(s: str) -> str:
    """Pre-quote a string literal for python-hcl2.

    python-hcl2 treats bare Python strings as raw HCL expressions.  String
    LITERALS must therefore arrive already surrounded by HCL double-quotes, with
    internal special characters escaped:
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


def hcl_literal(value: object) -> object:
    """Prepare a Python value for hcl2.dumps.

    python-hcl2 treats bare strings as raw HCL expressions, so string
    LITERALS must be pre-quoted, while VarRef expressions pass through raw.
    Dicts become nested object attrs; lists become HCL lists.
    """
    if isinstance(value, VarRef):
        return value.expr
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return _q(value)
    if isinstance(value, dict):
        return {k: hcl_literal(v) for k, v in value.items()}
    if isinstance(value, list):
        return [hcl_literal(v) for v in value]
    return value  # int/float/None — hcl2.dumps handles directly


def tofu_fmt(text: str, binary: str = "tofu") -> str:
    """Run `tofu fmt -` on *text* and return the formatted result."""
    proc = subprocess.run(
        [binary, "fmt", "-"],
        input=text,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip())
    return proc.stdout


def _render_lifecycle_raw(lifecycle: dict[str, object]) -> str:
    """Render a lifecycle block as a raw indented text block.

    python-hcl2 expands list values to multi-line, but the test requires
    ``ignore_changes = [attr_ref]`` on a single line (attr refs, not strings).
    We therefore bypass Builder/dumps for this block and write it directly;
    tofu fmt keeps single-line lists intact.
    """
    lines = ["  lifecycle {"]
    for k, v in lifecycle.items():
        if isinstance(v, list):
            refs = ", ".join(str(r) for r in v)
            lines.append(f"    {k} = [{refs}]")
    lines.append("  }")
    return "\n".join(lines)


def render_resource(
    resource_type: str,
    slug: str,
    attrs: dict[str, object],
    lifecycle: dict[str, object] | None = None,
    block_attrs: tuple[str, ...] = (),
) -> str:
    """Render a single Terraform/OpenTofu resource block to formatted HCL.

    Constructs handled:
    - Scalar / string attributes (string literals pre-quoted via ``_q``).
    - Nested object attribute: dict value → ``name = { … }`` HCL object.
    - List-of-object attribute: list-of-dict → ``name = [ { … }, … ]``.
    - Repeated nested blocks (``block_attrs``): each list entry → a separate
      ``name { … }`` block rather than an ``= […]`` assignment.
    - VarRef: rendered as a bare ``var.<name>`` traversal expression.
    - lifecycle: rendered as raw text so ``ignore_changes`` refs stay unquoted.
    """
    builder = Builder()

    # Scalar attrs and plain list/object attrs (everything except block_attrs).
    scalar_attrs = {
        k: hcl_literal(v)
        for k, v in attrs.items()
        if k not in block_attrs
    }
    block = builder.block("resource", [_q(resource_type), _q(slug)], **scalar_attrs)

    # Repeated nested blocks (nesting_mode=set/list in provider schema).
    for name in block_attrs:
        entries = attrs.get(name)
        if isinstance(entries, list):
            for entry in entries:
                if isinstance(entry, dict):
                    block.block(name, **{k: hcl_literal(v) for k, v in entry.items()})

    # Convert to HCL text and apply tofu fmt.
    hcl_text = tofu_fmt(hcl2.dumps(builder.build()))

    if lifecycle:
        # Splice the lifecycle block in before the closing `}` of the resource,
        # then re-format.  We build it as raw text to preserve bare identifier
        # refs inside ignore_changes (python-hcl2 would expand the list and
        # wrap refs in quotes, which is wrong).
        body = hcl_text.rstrip()          # strip trailing newline(s)
        if not body.endswith("}"):
            raise ValueError(f"unexpected hcl2.dumps tail: {body[-20:]!r}")
        body = body[:-1].rstrip()         # strip the closing "}"
        lifecycle_txt = _render_lifecycle_raw(lifecycle)
        combined = body + "\n\n" + lifecycle_txt + "\n}\n"
        hcl_text = tofu_fmt(combined)

    return hcl_text


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


def render_json_fallback(
    resource_type: str,
    slug: str,
    attrs: dict[str, object],
) -> str:
    """Emit a ``.tf.json`` resource block as a fallback.

    Used when python-hcl2 cannot render a construct correctly (e.g. for a
    resource type whose schema requires a construct that Builder/dumps
    mis-renders despite best efforts).  VarRef values become ``${var.name}``
    interpolation expressions, which are valid in .tf.json.
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
