import os
from pathlib import Path
from typing import IO, Any

from .cleaner import clean_resource, normalize_emitted
from .config import Config, resolve_api_key
from .controller import Controller
from .enumerator import ImportTarget, enumerate_controller
from .hcl_writer import render_resource
from .import_emitter import emit_import_blocks
from .manifest import spec_for_type
from .reporter import format_drift, format_gaps
from .secrets import resolve_secrets
from .tofu_runner import TofuRunner

# Repeated blocks that live in schema block_types -> render as blocks (C2).
BLOCK_ATTRS: dict[str, tuple[str, ...]] = {
    "unifi_device": ("port_override",),
    "unifi_wlan": ("schedule",),
    "unifi_radius_profile": ("acct_server", "auth_server"),
}


def _schema_for(schema: dict[str, Any], resource_type: str) -> dict[str, Any]:
    for prov in schema["provider_schemas"].values():
        rs = prov.get("resource_schemas", {})
        if resource_type in rs:
            return rs[resource_type]  # type: ignore[no-any-return]
    raise KeyError(resource_type)


def build_hcl(planned_values: dict[str, Any], schema: dict[str, Any]) -> str:
    parts = []
    resources = planned_values["planned_values"]["root_module"]["resources"]
    for res in resources:
        rtype = res["type"]
        slug = res["name"]          # M4: the import slug from generate-config-out
        rschema = _schema_for(schema, rtype)
        refs, lifecycle = resolve_secrets(rtype, slug, rschema)
        attrs = clean_resource(res["values"], rschema, sensitive=refs)
        attrs = normalize_emitted(rtype, attrs)
        parts.append(render_resource(
            rtype, slug, attrs,
            lifecycle=lifecycle or None,
            block_attrs=BLOCK_ATTRS.get(rtype, ()),
        ))
    return "\n".join(parts)


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
    out_file.write_text(build_hcl(planned, schema))
    # Replace the raw stub with our clean HCL: drop the stub so it does not
    # coexist as a second definition of the same resources (which `verify`'s
    # `tofu plan` would reject as a duplicate).
    (workdir / "generated_stub.tf").unlink(missing_ok=True)

    print(format_gaps(res.gaps), file=out)
    if mode == "incremental":
        print(
            f"Incremental: {len(targets)} new object(s) imported; "
            "drift on already-managed resources shows via `tofu plan`.",
            file=out,
        )
    return 0


def run_verify(cfg: Config, out: IO[str]) -> int:
    runner = TofuRunner(workdir=Path(cfg.workdir))
    code = runner.plan(out=Path(cfg.workdir) / "verify.plan")
    plan = runner.show_json(Path(cfg.workdir) / "verify.plan")
    print(format_drift(plan), file=out)
    return 0 if runner.is_clean(code) else 1


def _api_key(cfg: Config) -> str:
    return resolve_api_key(cfg, environ=os.environ)
