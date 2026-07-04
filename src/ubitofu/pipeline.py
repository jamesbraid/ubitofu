# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 James Braid
import os
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Any

from .cleaner import VarRef, clean_resource, normalize_emitted, strip_secret_shaped
from .config import Config, resolve_api_key
from .controller import Controller
from .enumerator import ImportTarget, enumerate_controller
from .hcl_surgeon import find_resource_block_span, update_scalar
from .hcl_writer import render_resource, render_variables
from .import_emitter import assign_slugs, emit_import_blocks
from .manifest import spec_for_type
from .reporter import (
    format_drift,
    format_gaps,
    format_reconcile,
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


def build_resource_attrs(
    res: dict[str, Any],
    schema: dict[str, Any],
    vault: str | None = None,
) -> tuple[str, dict[str, Any], dict[str, Any], list[str]]:
    """Clean one planned-values resource into emit-ready attrs (shared seam).

    Returns ``(slug, attrs, lifecycle, warnings)``. ``attrs`` have VarRefs
    substituted for sensitive values (never plaintext), sensitive attrs without
    a SECRETS rule dropped, per-resource value normalizations applied, and
    secret-shaped plaintext stripped into ``lifecycle["ignore_changes"]``.
    ``warnings`` are the ``<type>.<slug>: <path>`` lines for each stripped value.

    Used by both ``build()`` (wholesale generate) and ``run_reconcile`` (drift
    diff on ``change.before``/``after`` and new-object rendering).
    """
    rtype = res["type"]
    slug = res["name"]              # M4: the import slug from generate-config-out
    rschema = _schema_for(schema, rtype)
    refs, lifecycle, suppress = resolve_secrets(rtype, slug, rschema)
    attrs = clean_resource(res["values"], rschema, sensitive=refs)
    # Remove sensitive attrs that have no SECRETS rule — must not appear as
    # plaintext, and lifecycle.ignore_changes covers them against wipe.
    for attr in suppress:
        attrs.pop(attr, None)
    attrs = normalize_emitted(rtype, attrs)
    warnings: list[str] = []
    # Value-pattern safety net (the WireGuard lesson): the provider can
    # return secret material in plaintext with no schema sensitive flag.
    # Strip secret-shaped values, ignore their attrs, and warn loudly.
    for path in sorted(strip_secret_shaped(attrs)):
        top = re.split(r"[.\[]", path)[0]
        ignored = lifecycle.setdefault("ignore_changes", [])
        if top not in ignored:
            ignored.append(top)
        warnings.append(f"{rtype}.{slug}: {path}")
    return slug, attrs, lifecycle, warnings


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
        rschema = _schema_for(schema, rtype)
        slug, attrs, lifecycle, res_warnings = build_resource_attrs(res, schema, vault)
        warnings.extend(res_warnings)
        # Sensitive attrs are always substituted as top-level VarRefs (secrets.py
        # invariant), so scanning attrs recovers exactly the referenced vars.
        var_names.update(v.expr.removeprefix("var.")
                         for v in attrs.values() if isinstance(v, VarRef))
        if vault is not None:
            op_refs.update(secret_sources(rtype, slug, rschema, vault))
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


# Files that reconcile itself writes as scaffolding — never search/edit these
# as if they were operator-maintained committed config.
_RECONCILE_SCAFFOLD = frozenset({"generated_stub.tf", "unifi-variables.tf"})
_MISSING = object()


def _committed_tf_files(workdir: Path) -> list[Path]:
    return [p for p in sorted(workdir.glob("*.tf"))
            if p.name not in _RECONCILE_SCAFFOLD]


def _find_file_for(files: list[Path], rtype: str, slug: str) -> Path | None:
    """Return the committed file whose text declares resource TYPE.SLUG, if any."""
    for p in files:
        if find_resource_block_span(p.read_text(), rtype, slug) is not None:
            return p
    return None


def _is_scalar(v: object) -> bool:
    # bool is an int subclass — allowed; VarRef (secret ref) and containers are not.
    return isinstance(v, str | int | float | bool)


def _diff_resource(
    rtype: str,
    slug: str,
    live: dict[str, Any],
    committed: dict[str, Any],
    path: Path,
    merged: list[str],
    complex_flags: list[str],
) -> None:
    """Merge scalar drift into *path* in place; flag everything else.

    ``live`` and ``committed`` are both cleaned attr dicts (build_resource_attrs
    over change.before / change.after), so sensitive values are already VarRefs
    or suppressed and can never diff as plaintext.
    """
    text = path.read_text()
    changed = False
    for attr in sorted(set(live) | set(committed)):
        lv = live.get(attr, _MISSING)
        cv = committed.get(attr, _MISSING)
        if lv == cv:
            continue
        addr = f"{rtype}.{slug}.{attr}"
        if lv is _MISSING or cv is _MISSING:
            where = "absent on controller" if lv is _MISSING else "added on controller"
            complex_flags.append(f"{addr}: {where} — manual add/remove")
            continue
        if _is_scalar(lv) and _is_scalar(cv):
            try:
                text = update_scalar(text, rtype, slug, attr, cv, lv)
            except (LookupError, ValueError) as exc:
                complex_flags.append(f"{addr}: could not edit in place ({exc})")
                continue
            merged.append(f"{addr}: {cv!r} -> {lv!r}")
            changed = True
        else:
            complex_flags.append(f"{addr}: nested/list/map drift — manual review")
    if changed:
        path.write_text(text)


def _import_block(rtype: str, slug: str, import_id: str) -> str:
    return f'import {{\n  to = {rtype}.{slug}\n  id = "{import_id}"\n}}'


_RESOURCE_HDR_RE = re.compile(r'^resource\s+"([^"]+)"\s+"([^"]+)"', re.MULTILINE)


def _committed_addresses(committed_files: list[Path]) -> set[str]:
    """Return 'type.slug' for every resource block present in committed *.tf files."""
    addrs: set[str] = set()
    for p in committed_files:
        for m in _RESOURCE_HDR_RE.finditer(p.read_text()):
            addrs.add(f"{m.group(1)}.{m.group(2)}")
    return addrs


def _state_addresses(runner: TofuRunner) -> set[str]:
    """Return 'type.name' for every resource tracked in tofu state."""
    state = runner.show_state_json()
    root = state.get("values", {}).get("root_module", {})
    return {f"{r['type']}.{r['name']}" for r in root.get("resources", [])}


def run_reconcile(cfg: Config, mode: str, out: IO[str]) -> int:
    """Surgically merge committed HCL toward live controller state.

    Unlike generate (wholesale regenerate, comments dropped), reconcile edits the
    operator's committed *.tf in place: drifted top-level scalars are updated
    (comments/layout preserved), new controller objects are appended with their
    import blocks, and complex drift + controller-side removals are flagged for
    manual review. The report is the product; exit is always 0.
    """
    ctl = Controller(base_url=cfg.controller_url, site=cfg.site, api_key=_api_key(cfg))
    res = enumerate_controller(ctl)
    workdir = Path(cfg.workdir)
    runner = TofuRunner(workdir=workdir)
    targets = res.targets

    # Seed slug assignment with addresses already in committed config + state so
    # a new object never steals a slug owned by a managed resource.
    committed_files = _committed_tf_files(workdir)
    reserved = _committed_addresses(committed_files) | _state_addresses(runner)
    slug_assignment = assign_slugs(targets, reserved=reserved)

    # Prelude (shared with generate): one plan whose show-json gives both
    # resource_changes (change.before = LIVE, change.after = committed) for
    # already-managed resources, and planned_values (live, schema-shaped) for
    # newly-imported objects. import/refresh are forbidden; plan(-out)->show_json
    # is the only idiom.
    #
    # Use a unique tempfile in the workdir (tofu reads only *.tf in the module
    # dir, so /tmp doesn't work; leading-dot names are also skipped). Deleted in
    # try/finally so a crashed plan never leaves a stale file behind. The
    # operator's imports.tf is never touched.
    fd, scratch = tempfile.mkstemp(dir=workdir, prefix="ubitofu-reconcile-", suffix=".tf")
    os.close(fd)
    try:
        Path(scratch).write_text(
            "\n\n".join(_import_block(t.resource_type, s, t.import_id)
                        for t, s in slug_assignment) + "\n"
        )
        (workdir / "generated_stub.tf").unlink(missing_ok=True)
        runner.plan(out=workdir / "tf.plan",
                    generate_config_out=workdir / "generated_stub.tf")
        schema = runner.providers_schema()
        plan = runner.show_json(workdir / "tf.plan")
        (workdir / "generated_stub.tf").unlink(missing_ok=True)
    finally:
        Path(scratch).unlink(missing_ok=True)
    merged: list[str] = []
    complex_flags: list[str] = []
    appended: list[str] = []
    diverged: list[tuple[str, str]] = []
    orphaned: list[str] = []

    # --- Drift + removals on already-managed resources (resource_changes) ---
    for rc in plan.get("resource_changes", []):
        actions = rc.get("change", {}).get("actions", [])
        rtype, slug = rc["type"], rc["name"]
        if actions in (["no-op"], ["read"]):
            continue
        path = _find_file_for(committed_files, rtype, slug)
        if actions == ["update"]:
            if path is None:
                continue  # a fresh import (canonical slug); handled by append below
            change = rc["change"]
            _, live_attrs, _, _ = build_resource_attrs(
                {"type": rtype, "name": slug, "values": change.get("before") or {}},
                schema, cfg.op_vault)
            _, committed_attrs, _, _ = build_resource_attrs(
                {"type": rtype, "name": slug, "values": change.get("after") or {}},
                schema, cfg.op_vault)
            _diff_resource(rtype, slug, live_attrs, committed_attrs, path,
                           merged, complex_flags)
        elif path is not None and (
                "create" in actions or "delete" in actions or "replace" in actions):
            # In committed config but plan diverged — classify so the operator
            # knows whether to apply, re-adopt, or investigate.
            change = rc.get("change", {})
            before = change.get("before")
            after = change.get("after")
            if actions == ["delete"] or (before is not None and after is None):
                tag = "deleted"
            elif actions == ["create"] or before is None:
                tag = "pending"
            else:
                tag = "diverged"
            diverged.append((f"{rtype}.{slug}", tag))
        elif path is None and (
                "create" in actions or "delete" in actions or "replace" in actions):
            # In state but absent from committed config — tofu would DESTROY on apply.
            orphaned.append(f"{rtype}.{slug} — in state but not in committed config")

    # --- New controller objects: append resource + import block ---
    # Full-list slug assignment (reserved-seeded above) so appended slugs never
    # collide with already-managed resources.
    slug_by_key = {(t.resource_type, t.import_id): s for t, s in slug_assignment}
    new = new_targets(targets, state_identities(runner))
    live_new: dict[tuple[str, str], tuple[dict[str, Any], dict[str, Any]]] = {}
    for pv in plan.get("planned_values", {}).get("root_module", {}).get("resources", []):
        pslug, pattrs, plifecycle, _pv_warnings = build_resource_attrs(pv, schema, cfg.op_vault)
        live_new[(pv["type"], pslug)] = (pattrs, plifecycle)

    secret_var_names: list[str] = []
    new_blocks: list[str] = []
    new_imports: list[str] = []
    for t in new:
        slug = slug_by_key[(t.resource_type, t.import_id)]
        # Idempotence: skip anything already declared in committed config
        # (including a prior run's reconciled_new.tf).
        if _find_file_for(committed_files, t.resource_type, slug) is not None:
            continue
        entry = live_new.get((t.resource_type, slug))
        if entry is None:
            complex_flags.append(
                f"{t.resource_type}.{slug} — new object but no planned values; "
                "run generate")
            continue
        attrs, lifecycle = entry
        # Collect secret var names from VarRefs in attrs of actually-appended objects.
        for v in attrs.values():
            if isinstance(v, VarRef):
                vname = v.expr.removeprefix("var.")
                if vname not in secret_var_names:
                    secret_var_names.append(vname)
        rschema = _schema_for(schema, t.resource_type)
        new_blocks.append(render_resource(
            t.resource_type, slug, attrs, lifecycle=lifecycle or None,
            block_attrs=tuple(rschema["block"].get("block_types", {}))))
        new_imports.append(_import_block(t.resource_type, slug, t.import_id))
        appended.append(f"{t.resource_type}.{slug} ({t.import_id})")

    if new_blocks:
        nf = workdir / "reconciled_new.tf"
        existing = nf.read_text() if nf.exists() else ""
        if existing.strip():
            prefix = existing.rstrip() + "\n\n"
        else:
            prefix = (
                "# Generated by ubitofu reconcile"
                " — newly-adopted objects + their import blocks.\n"
                "# import {} blocks are transient:"
                " once applied they are inert and may be deleted.\n\n"
            )
        # Self-contained entries: resource block immediately followed by its
        # import block. reconcile owns this file; the operator's imports.tf
        # is never written.
        entries = [f"{block}\n\n{imp}" for block, imp in zip(new_blocks, new_imports)]
        nf.write_text(prefix + "\n\n".join(entries) + "\n")
        if secret_var_names:
            write_variables_tf(workdir, secret_var_names, merge=True)
    (workdir / "tf.plan").unlink(missing_ok=True)

    print(format_reconcile(merged, complex_flags, appended,
                           secret_warnings=secret_var_names or None,
                           orphaned=orphaned or None,
                           diverged=diverged or None), file=out)
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
