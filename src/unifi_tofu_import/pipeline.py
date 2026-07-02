import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Any

from .cleaner import clean_resource, normalize_emitted, strip_secret_shaped
from .config import Config, resolve_api_key
from .controller import Controller
from .enumerator import ImportTarget, enumerate_controller
from .hcl_writer import render_resource, render_variables
from .import_emitter import emit_import_blocks
from .manifest import spec_for_type
from .reporter import (
    format_drift,
    format_gaps,
    format_secret_sources,
    format_secret_suppressions,
    is_secrets_only_diff,
)
from .secrets import resolve_secrets, secret_sources, sensitive_attrs
from .tofu_runner import TofuRunner


def _schema_for(schema: dict[str, Any], resource_type: str) -> dict[str, Any]:
    for prov in schema["provider_schemas"].values():
        rs = prov.get("resource_schemas", {})
        if resource_type in rs:
            return rs[resource_type]  # type: ignore[no-any-return]
    raise KeyError(resource_type)


@dataclass
class BuildResult:
    hcl: str
    # "<resource_type>.<slug>: <attr path>" per secret-shaped value suppressed
    # by the value-pattern safety net (sorted per resource, deterministic).
    secret_warnings: list[str] = field(default_factory=list)
    # Every var.<name> the emitted HCL references (sorted, deduped) …
    var_names: list[str] = field(default_factory=list)
    # … and, when a vault was given, where each value should come from.
    op_refs: dict[str, str] = field(default_factory=dict)


def build(
    planned_values: dict[str, Any],
    schema: dict[str, Any],
    vault: str | None = None,
) -> BuildResult:
    parts = []
    warnings: list[str] = []
    var_names: set[str] = set()
    op_refs: dict[str, str] = {}
    resources = planned_values["planned_values"]["root_module"]["resources"]
    for res in resources:
        rtype = res["type"]
        slug = res["name"]          # M4: the import slug from generate-config-out
        rschema = _schema_for(schema, rtype)
        refs, lifecycle, suppress = resolve_secrets(rtype, slug, rschema)
        var_names.update(r.expr.removeprefix("var.") for r in refs.values())
        if vault is not None:
            op_refs.update(secret_sources(rtype, slug, rschema, vault))
        attrs = clean_resource(res["values"], rschema, sensitive=refs)
        # Remove sensitive attrs that have no SECRETS rule — must not appear as
        # plaintext, and lifecycle.ignore_changes covers them against wipe.
        for attr in suppress:
            attrs.pop(attr, None)
        attrs = normalize_emitted(rtype, attrs)
        # Value-pattern safety net (the WireGuard lesson): the provider can
        # return secret material in plaintext with no schema sensitive flag.
        # Strip secret-shaped values, ignore their attrs, and warn loudly.
        for path in sorted(strip_secret_shaped(attrs)):
            top = re.split(r"[.\[]", path)[0]
            ignored = lifecycle.setdefault("ignore_changes", [])
            if top not in ignored:
                ignored.append(top)
            warnings.append(f"{rtype}.{slug}: {path}")
        parts.append(render_resource(
            rtype, slug, attrs,
            lifecycle=lifecycle or None,
            # Repeated blocks live in schema block_types -> render as blocks (C2).
            block_attrs=tuple(rschema["block"].get("block_types", {})),
        ))
    return BuildResult(hcl="\n".join(parts), secret_warnings=warnings,
                       var_names=sorted(var_names), op_refs=op_refs)


def build_hcl(planned_values: dict[str, Any], schema: dict[str, Any]) -> str:
    return build(planned_values, schema).hcl


_VARIABLE_DECL_RE = re.compile(r'^variable\s+"([^"]+)"', re.MULTILINE)


def write_variables_tf(workdir: Path, var_names: list[str], merge: bool) -> None:
    """Write unifi-variables.tf so the generated config is self-contained.

    Bulk regenerates the whole config, so the declaration set is rewritten
    outright; incremental only appends resources, so existing declarations
    are kept and merged with the new ones.
    """
    vf = workdir / "unifi-variables.tf"
    names = set(var_names)
    if merge and vf.exists():
        names.update(_VARIABLE_DECL_RE.findall(vf.read_text()))
    vf.write_text(render_variables(sorted(names)))


def _identity(id_rule: str, values: dict[str, Any]) -> str | None:
    # Same per-type strategy as enumerator.extract_id, applied to a STATE row.
    if id_rule == "mac":
        return values.get("mac")
    if id_rule == "mac_or_id":
        return values.get("mac") or values.get("id")
    # "_id", "site", "site:_id", "wg_two_level" -> provider stores identity in `id`
    return values.get("id")


def state_identities(runner: TofuRunner) -> dict[str, set[str]]:
    state = runner.show_state_json()
    root = state.get("values", {}).get("root_module", {})
    out: dict[str, set[str]] = {}
    for r in root.get("resources", []):
        rtype = r["type"]
        try:
            rule = spec_for_type(rtype).id_rule
        except KeyError:
            continue
        ident = _identity(rule, r.get("values", {}))
        if ident is not None:
            out.setdefault(rtype, set()).add(ident)
    return out


def new_targets(
    targets: list[ImportTarget],
    managed: dict[str, set[str]],
) -> list[ImportTarget]:
    return [t for t in targets
            if t.import_id not in managed.get(t.resource_type, set())]


def run_generate(cfg: Config, mode: str, out: IO[str]) -> int:
    ctl = Controller(base_url=cfg.controller_url, site=cfg.site,
                     api_key=_api_key(cfg))
    res = enumerate_controller(ctl)
    workdir = Path(cfg.workdir)
    runner = TofuRunner(workdir=workdir)

    targets = res.targets
    if mode == "incremental":
        targets = new_targets(targets, state_identities(runner))

    # Bulk overwrites the whole config; incremental writes ONLY the new
    # resources to a separate *.tf (OpenTofu loads every *.tf, so this is
    # "appended to the config") — never clobbering already-managed HCL.
    out_file = workdir / ("generated.tf" if mode == "bulk" else "generated_new.tf")
    (workdir / "imports.tf").write_text(emit_import_blocks(targets))
    # M7: tofu refuses to overwrite an existing -generate-config-out file, and
    # would also error if a prior run's out_file already declares a resource
    # our import block re-imports. Scratch both so re-runs (and every
    # incremental run) start clean.
    (workdir / "generated_stub.tf").unlink(missing_ok=True)
    out_file.unlink(missing_ok=True)
    runner.plan(out=workdir / "tf.plan",
                generate_config_out=workdir / "generated_stub.tf")
    schema = runner.providers_schema()
    planned = runner.show_json(workdir / "tf.plan")
    result = build(planned, schema, vault=cfg.op_vault)
    out_file.write_text(result.hcl)
    # Declare every referenced var so the output is self-contained; values
    # come from the operator's secret manager (refs printed below, never
    # written to files).
    write_variables_tf(workdir, result.var_names, merge=(mode == "incremental"))
    # Replace the raw stub with our clean HCL: drop the stub so it does not
    # coexist as a second definition of the same resources (which `verify`'s
    # `tofu plan` would reject as a duplicate).
    (workdir / "generated_stub.tf").unlink(missing_ok=True)

    print(format_gaps(res.gaps), file=out)
    if result.op_refs:
        print(format_secret_sources(result.op_refs), file=out)
    if result.secret_warnings:
        print(format_secret_suppressions(result.secret_warnings), file=out)
    if mode == "incremental":
        print(
            f"Incremental: {len(targets)} new object(s) imported; "
            "drift on already-managed resources shows via `tofu plan`.",
            file=out,
        )
    return 0


def _sensitive_map(schema: dict[str, Any]) -> dict[str, set[str]]:
    """Schema-derived {resource_type: sensitive/write_only attr names}."""
    out: dict[str, set[str]] = {}
    for prov in schema["provider_schemas"].values():
        for rtype, rschema in prov.get("resource_schemas", {}).items():
            out[rtype] = sensitive_attrs(rschema)
    return out


def run_verify(cfg: Config, out: IO[str]) -> int:
    runner = TofuRunner(workdir=Path(cfg.workdir))
    code = runner.plan(out=Path(cfg.workdir) / "verify.plan")
    plan = runner.show_json(Path(cfg.workdir) / "verify.plan")
    if runner.is_clean(code):
        print(format_drift(plan), file=out)
        return 0
    # Exit 2 = changes present. Secret attrs are sourced from vars the plan
    # cannot see into, so a diff confined to schema-sensitive attrs is expected
    # and passes; anything else is real drift.
    if is_secrets_only_diff(plan, _sensitive_map(runner.providers_schema())):
        print("Drift: secrets-only diff (schema-sensitive attrs) — pass.", file=out)
        return 0
    print(format_drift(plan), file=out)
    return 1


def _api_key(cfg: Config) -> str:
    return resolve_api_key(cfg, environ=os.environ)
