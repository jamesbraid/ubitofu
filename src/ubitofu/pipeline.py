# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 James Braid
import os
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Any

from deepdiff import DeepDiff

from .cleaner import VarRef, clean_resource, normalize_emitted, strip_secret_shaped
from .config import Config
from .controller import Controller, controller_from_config
from .coverage import audit, write_coverage_md
from .enumerator import ImportTarget, derive_identity, enumerate_controller
from .hcl_surgeon import delete_resource_block, find_resource_block_span, update_scalar
from .hcl_writer import render_resource, render_variables
from .import_emitter import assign_slugs, emit_import_blocks
from .manifest import spec_for_type
from .reporter import (
    format_coverage,
    format_drift,
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


# Outcome exit codes, rsync-style flat enumeration, shared by reconcile and
# verify (cli.py documents them in every subcommand's --help epilog). Errors
# exit 1 via the CLI; usage errors exit 2 (argparse).
EXIT_DRIFT_CAPTURED = 10       # drift captured — files edited or appended
EXIT_ATTENTION = 11            # operator attention required — flags / drift
EXIT_DRIFT_AND_ATTENTION = 12  # both of the above in one run
EXIT_FORBIDDEN_CREATE = 13     # planned unifi_device create — UI-only lifecycle


def _identity(id_rule: str, values: dict[str, Any], site: str = "") -> str | None:
    """Derive identity from a tofu state row.

    Delegates to derive_identity (the single source of truth) so this function
    and extract_id on the controller side are structurally guaranteed to agree
    for every id_rule — drift between the two is impossible.

    ``site`` must be supplied for resources with id_rule=="site" (singletons);
    callers that pass the site from Config ensure the match is exact.
    """
    return derive_identity(id_rule, values, site)


def state_identities(runner: TofuRunner, site: str = "") -> dict[str, set[str]]:
    """Return {resource_type: set(import_ids)} for every resource in tofu state.

    ``site`` should be the configured site name; it is forwarded to
    _identity so site-singleton resources (id_rule=="site") are recognised
    correctly.
    """
    state = runner.show_state_json()
    root = state.get("values", {}).get("root_module", {})
    out: dict[str, set[str]] = {}
    for r in root.get("resources", []):
        rtype = r["type"]
        try:
            rule = spec_for_type(rtype).id_rule
        except KeyError:
            continue
        ident = _identity(rule, r.get("values", {}), site)
        if ident is not None:
            out.setdefault(rtype, set()).add(ident)
    return out


def new_targets(
    targets: list[ImportTarget],
    managed: dict[str, set[str]],
) -> list[ImportTarget]:
    return [t for t in targets
            if t.import_id not in managed.get(t.resource_type, set())]


def _state_identity_by_address(runner: TofuRunner, site: str = "") -> dict[str, str]:
    """Map "type.slug" -> import_id for every state resource with a known rule.

    Sibling of state_identities keyed by address instead of pooled by type, so
    a specific plan entry can be matched back to the identity it had when last
    applied (an _id-ruled resource's committed values never carry the id).
    """
    state = runner.show_state_json()
    root = state.get("values", {}).get("root_module", {})
    out: dict[str, str] = {}
    for r in root.get("resources", []):
        rtype = r["type"]
        try:
            rule = spec_for_type(rtype).id_rule
        except KeyError:
            continue
        ident = _identity(rule, r.get("values", {}), site)
        if ident is not None:
            out[f"{rtype}.{r['name']}"] = ident
    return out


def _state_values_by_address(runner: TofuRunner) -> dict[str, dict[str, Any]]:
    """Map "type.slug" -> raw state values (the last-applied snapshot).

    The three-way oracle for _diff_resource: last-applied state disambiguates
    controller drift (live diverged from what was applied) from unapplied
    config intent (committed diverged from what was applied, live has not
    caught up yet).
    """
    state = runner.show_state_json()
    root = state.get("values", {}).get("root_module", {})
    return {f"{r['type']}.{r['name']}": r.get("values", {})
            for r in root.get("resources", [])}


def classify_diverged(
    rtype: str,
    change: dict[str, Any],
    live_identities: dict[str, set[str]],
    site: str = "",
    state_identity: str | None = None,
) -> str:
    """Classify a committed-config resource whose plan diverged.

    A plan ``create`` alone cannot distinguish "merged but not yet applied"
    from "object deleted on the controller" — both have before=None. The live
    enumeration disambiguates: identity comes from the state row when the
    resource was applied before (``state_identity``), else from the committed
    values (devices carry their MAC in config). A derivable-but-absent
    identity only means "gone" when tofu could not have created it anyway:
    the resource was applied before (state identity exists) or the type is
    UI-lifecycle (controller-adopted, e.g. unifi_device) and tofu can never
    create it. Otherwise absence just means "not created yet" — apply will
    create it.

    Tags (rendered by reporter._DIVERGED_LABELS):
    - "deleted":  gone on controller (or uncreatable) — remove from config or re-adopt
    - "pending":  not yet applied, for one of three reasons — present live
                  (derivable identity found in the live enumeration), absence
                  unprovable (identity underivable, so gone-vs-not can't be
                  told apart — the conservative default), or apply will
                  create it (derivable identity, genuinely absent, never
                  applied, not UI-lifecycle)
    - "diverged": anything else (e.g. replace)
    """
    actions = change.get("actions") or []
    before = change.get("before")
    after = change.get("after")
    if actions == ["delete"] or (before is not None and after is None):
        return "deleted"
    if actions == ["create"] or before is None:
        try:
            spec = spec_for_type(rtype)
        except KeyError:
            return "pending"
        ident = state_identity
        if ident is None:
            ident = _identity(spec.id_rule, after or {}, site)
        absent = ident is not None and ident not in live_identities.get(rtype, set())
        if absent and (state_identity is not None or spec.ui_lifecycle):
            # Gone from the controller and either previously applied or a
            # UI-lifecycle type tofu can never create: the block must go.
            return "deleted"
        return "pending"
    return "diverged"


def _emit_coverage(
    ctl: Controller,
    schema: dict[str, Any],
    workdir: Path,
    enum_gaps: list[str],
    out: IO[str],
    check: bool = False,
) -> None:
    """Audit provider coverage, persist COVERAGE.md, print the section.

    Called by reconcile and generate after the schema fetch. COVERAGE.md is
    the acceptance ledger: a changed file rides the nightly drift PR, so new
    gaps notify and closures are visible. ``check`` skips the write (the
    apply gate never touches the tree) but still prints the section.
    """
    report = audit(ctl, schema)
    if not check:
        write_coverage_md(workdir, report)
    print(format_coverage(enum_gaps + report.gap_lines(),
                          len(report.accepted)), file=out)


def run_generate(cfg: Config, mode: str, out: IO[str]) -> int:
    ctl = controller_from_config(cfg)
    try:
        res = enumerate_controller(ctl)
        workdir = Path(cfg.workdir)
        runner = TofuRunner(workdir=workdir)

        targets = res.targets
        if mode == "incremental":
            targets = new_targets(targets, state_identities(runner, cfg.site))

        # Bulk overwrites the whole config; incremental writes ONLY the new
        # resources to a separate *.tf (OpenTofu loads every *.tf, so this is
        # "appended to the config") — never clobbering already-managed HCL.
        out_file = workdir / ("generated.tf" if mode == "bulk" else "generated_new.tf")
        (workdir / "imports.tf").write_text(emit_import_blocks(targets))
        # M7: tofu refuses to overwrite an existing -generate-config-out file,
        # and would also error if a prior run's out_file already declares a
        # resource our import block re-imports. Scratch both so re-runs (and
        # every incremental run) start clean.
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
        # Replace the raw stub with our clean HCL: drop the stub so it does
        # not coexist as a second definition of the same resources (which
        # `verify`'s `tofu plan` would reject as a duplicate).
        (workdir / "generated_stub.tf").unlink(missing_ok=True)

        _emit_coverage(ctl, schema, workdir, res.gaps, out)
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
    finally:
        ctl.close()


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


def _friendly_deepdiff_path(path: str) -> str:
    """Convert a deepdiff path string to a human-readable attribute path.

    Examples:
        root['port_override'][0]['forward']  →  port_override[0].forward
        root['start']                        →  start
        root[0]                              →  (empty — the root element itself)
    """
    # Replace string-key segments ['key'] or ["key"] with .key; integer indices [N] stay
    friendly = re.sub(r"\[(['\"])([^'\"]+)\1\]", r".\2", path)
    # Strip the leading "root" sentinel and any resulting leading dot
    return friendly.removeprefix("root").lstrip(".")


def reconcile_complex_flags(
    live: dict[str, Any],
    committed: dict[str, Any],
    addr: str,
) -> list[str]:
    """Return precise flag strings for drift that reconcile cannot auto-edit.

    Walks live/committed attr dicts:
    - Absent/added attrs → manual add/remove flag.
    - Scalar diffs → silently skipped (handled by update_scalar in _diff_resource).
    - Non-scalar diffs → expanded by DeepDiff into per-path old→new strings, e.g.
      ``unifi_device.x.port_override[0].forward: 'native' → 'customize' — manual review``.

    ``live`` is the controller state, ``committed`` is what's in HCL.
    ``addr`` is the resource address prefix, e.g. ``unifi_device.x``.
    """
    # Map internal deepdiff change-type keys to user-facing phrases.
    _CHANGE_PHRASES: dict[str, str] = {
        "type_changes": "type or value changed",
        "iterable_item_added": "added",
        "iterable_item_removed": "removed",
        "dictionary_item_added": "added",
        "dictionary_item_removed": "removed",
        "attribute_added": "added",
        "attribute_removed": "removed",
        "set_item_added": "added",
        "set_item_removed": "removed",
    }

    flags: list[str] = []
    for attr in sorted(set(live) | set(committed)):
        lv = live.get(attr, _MISSING)
        cv = committed.get(attr, _MISSING)
        if lv == cv:
            continue
        full_addr = f"{addr}.{attr}"
        if lv is _MISSING or cv is _MISSING:
            where = "absent on controller" if lv is _MISSING else "added on controller"
            flags.append(f"{full_addr}: {where} — manual add/remove")
            continue
        if _is_scalar(lv) and _is_scalar(cv):
            continue  # scalar: handled by update_scalar, not flagged here
        try:
            diff = DeepDiff(cv, lv, verbose_level=2)
            if not diff:
                # Values compare equal under deepdiff despite differing under ==
                # (e.g. type coercions) — fall back to a generic flag.
                flags.append(f"{full_addr}: nested/list/map drift — manual review")
                continue
            for change_type, changes in diff.items():
                for dpath, change_val in changes.items():
                    friendly = _friendly_deepdiff_path(dpath)
                    # Friendly may start with '[' (integer index at root) or be empty
                    if not friendly:
                        full_path = full_addr
                    elif friendly.startswith("["):
                        full_path = f"{full_addr}{friendly}"
                    else:
                        full_path = f"{full_addr}.{friendly}"
                    if change_type == "values_changed":
                        old_v = change_val["old_value"]
                        new_v = change_val["new_value"]
                        flags.append(
                            f"{full_path}: {old_v!r} → {new_v!r} — manual review"
                        )
                    elif change_type in _CHANGE_PHRASES:
                        phrase = _CHANGE_PHRASES[change_type]
                        if change_type.endswith("_added") or change_type.endswith("_removed"):
                            flags.append(
                                f"{full_path}: {phrase} {change_val!r} — manual review"
                            )
                        else:
                            flags.append(f"{full_path}: {phrase} — manual review")
                    else:
                        flags.append(f"{full_path}: changed — manual review")
        except Exception:
            # DeepDiff can raise on unhashable types or unusual controller payloads.
            # Degrade gracefully: emit the generic flag and continue; one bad
            # resource must never abort the whole reconcile run.
            flags.append(f"{full_addr}: nested/list/map drift — manual review")
    return flags


def _diff_resource(
    rtype: str,
    slug: str,
    live: dict[str, Any],
    committed: dict[str, Any],
    path: Path,
    merged: list[str],
    complex_flags: list[str],
    state_attrs: dict[str, Any] | None = None,
    check: bool = False,
) -> None:
    """Merge scalar drift into *path* in place; flag everything else.

    ``live`` and ``committed`` are both cleaned attr dicts (build_resource_attrs
    over change.before / change.after), so sensitive values are already VarRefs
    or suppressed and can never diff as plaintext.

    Scalar diffs go through update_scalar (comment-preserving surgeon).
    All other drift — absent/added attrs, nested blocks, lists, maps — is
    handed to reconcile_complex_flags which uses DeepDiff to produce precise
    per-path old→new flag strings.

    ``state_attrs``, when given, is the last-applied snapshot (also a cleaned
    attr dict, via build_resource_attrs over the tofu state row) and turns
    each scalar comparison three-way: state is the oracle that tells drift
    (live moved, state==committed) apart from unapplied config intent
    (committed moved, live==state — leave it for `apply`, never revert it)
    and flags real conflicts (all three differ) instead of guessing. ``None``
    preserves the old two-way behavior; an attr absent from ``state_attrs``
    (e.g. legacy state rows carrying only ``{"id": ...}``) falls back to it too.

    ``check``, when true, still classifies every scalar merge into ``merged``
    but skips the ``path.write_text`` — the apply gate's dry run.
    """
    text = path.read_text()
    changed = False
    for attr in sorted(set(live) | set(committed)):
        lv = live.get(attr, _MISSING)
        cv = committed.get(attr, _MISSING)
        if lv == cv:
            continue
        # Only auto-edit scalar→scalar changes; everything else is flagged below.
        if lv is _MISSING or cv is _MISSING:
            continue
        if not (_is_scalar(lv) and _is_scalar(cv)):
            continue
        addr = f"{rtype}.{slug}.{attr}"
        if state_attrs is not None and attr in state_attrs:
            sv = state_attrs[attr]
            if cv != sv and lv == sv:
                continue  # unapplied config intent — apply's job, not ours
            # The L == C cell (captured but unapplied) never reaches here:
            # the loop's lv == cv skip above already consumed it.
            if cv != sv and lv != sv:
                complex_flags.append(
                    f"{addr}: conflict — live {lv!r}, last applied {sv!r}, "
                    f"committed {cv!r} — manual review")
                continue
            # fall through: controller drift (live != state == committed) — capture
        try:
            text = update_scalar(text, rtype, slug, attr, cv, lv)
        except (LookupError, ValueError) as exc:
            complex_flags.append(f"{addr}: could not edit in place ({exc})")
            continue
        merged.append(f"{addr}: {cv!r} -> {lv!r}")
        changed = True
    # Precise flags for all non-scalar drift (absent/added + deepdiff paths)
    complex_flags.extend(reconcile_complex_flags(live, committed, f"{rtype}.{slug}"))
    if changed and not check:
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


# Matches the import block format produced by _import_block:
#   import {
#     to = TYPE.slug
#     id = "IMPORT_ID"
#   }
_EMITTED_IMPORT_RE = re.compile(
    r'import\s*\{\s*to\s*=\s*(\w+)\.\w+\s+id\s*=\s*"([^"]+)"\s*\}',
    re.DOTALL,
)


def _emitted_identities(workdir: Path) -> dict[str, set[str]]:
    """Parse reconciled_new.tf for import_ids already emitted, keyed by resource type.

    A subsequent reconcile run skips any target whose import_id already appears
    here — matched by stable id, not slug — so a prior run's output is never
    re-appended under a shifted slug when the slug space grows.
    """
    nf = workdir / "reconciled_new.tf"
    if not nf.exists():
        return {}
    text = nf.read_text()
    out: dict[str, set[str]] = {}
    for m in _EMITTED_IMPORT_RE.finditer(text):
        rtype, import_id = m.group(1), m.group(2)
        out.setdefault(rtype, set()).add(import_id)
    return out


def run_reconcile(cfg: Config, out: IO[str], check: bool = False) -> int:
    """Surgically merge committed HCL toward live controller state.

    Unlike generate (wholesale regenerate, comments dropped), reconcile edits the
    operator's committed *.tf in place: drifted top-level scalars are updated
    (comments/layout preserved), new controller objects are appended with their
    import blocks, and complex drift + controller-side removals are flagged for
    manual review. The report is the product; the return value encodes the
    outcome for scripting, rsync-style: 0 in sync, 10 drift captured, 11
    attention flagged, 12 both, 13 a planned unifi_device create (adoption is
    UI-only; 13 takes precedence over every other outcome) (errors surface as
    1 via the CLI).

    ``check``, when true, classifies and reports exactly as a wet run but
    writes nothing to the tree — every scalar merge, staged deletion,
    ``reconciled_new.tf``/``unifi-variables.tf`` append, and COVERAGE.md
    refresh is skipped while ``merged``/``removed``/``codified``/``appended``
    and the exit code stay identical. This is the apply gate's oracle: CI
    runs ``reconcile --check`` and branches on the exit code without ever
    mutating the tree.
    """
    ctl = controller_from_config(cfg)
    try:
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
        removed: list[str] = []
        codified: list[str] = []
        forbidden: list[str] = []
        # path -> post-deletion text; overlays file contents for the dangling-
        # reference scan so check mode (which writes nothing) sees staged state.
        staged_texts: dict[Path, str] = {}
        # Appended below by both the new-object loop and orphan codification;
        # codified entries append a block but never an import (already in state).
        new_blocks: list[str] = []
        new_imports: list[str] = []
        # Populated by both the codification branch and the new-object loop below
        # (a resource can only take one path, but both scan their own attrs).
        secret_var_names: list[str] = []

        # Live + last-applied identities for the diverged classification below.
        live_identities: dict[str, set[str]] = {}
        for t in targets:
            live_identities.setdefault(t.resource_type, set()).add(t.import_id)
        state_idents = _state_identity_by_address(runner, cfg.site)
        # Last-applied snapshot, keyed the same way: the three-way oracle _diff_resource
        # uses to tell controller drift apart from unapplied config intent.
        state_values = _state_values_by_address(runner)

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
                sv = state_values.get(f"{rtype}.{slug}")
                state_attrs = None
                if sv is not None:
                    _, state_attrs, _, _ = build_resource_attrs(
                        {"type": rtype, "name": slug, "values": sv},
                        schema, cfg.op_vault)
                _diff_resource(rtype, slug, live_attrs, committed_attrs, path,
                               merged, complex_flags, state_attrs=state_attrs,
                               check=check)
            elif path is not None and (
                    "create" in actions or "delete" in actions or "replace" in actions):
                # In committed config but plan diverged — classify so the operator
                # knows whether to apply, remove the block, or investigate.
                tag = classify_diverged(
                    rtype, rc.get("change", {}), live_identities, cfg.site,
                    state_identity=state_idents.get(f"{rtype}.{slug}"))
                if tag == "deleted":
                    # Controller-authoritative existence: stage the block removal
                    # in the drift working tree; the PR diff is the review surface.
                    # Track the post-deletion text (even in check mode) so the
                    # dangling-reference scan below sees the staged result.
                    text = staged_texts.get(path, path.read_text())
                    staged_texts[path] = delete_resource_block(text, rtype, slug)
                    if not check:
                        path.write_text(staged_texts[path])
                    removed.append(f"{rtype}.{slug}")
                else:
                    diverged.append((f"{rtype}.{slug}", tag))
            elif path is None and (
                    "create" in actions or "delete" in actions or "replace" in actions):
                # In state but absent from committed config — tofu would DESTROY on
                # apply, unless it is also live: then it was imported but never
                # committed (the state-only-orphan cell) and gets codified instead.
                ident = state_idents.get(f"{rtype}.{slug}")
                if ident is not None and ident in live_identities.get(rtype, set()):
                    # Live but state-only (never committed): codify instead of
                    # letting the next apply destroy it. Already in state, so no
                    # import block is needed.
                    _, attrs, lifecycle, _ = build_resource_attrs(
                        {"type": rtype, "name": slug,
                         "values": rc.get("change", {}).get("before") or {}},
                        schema, cfg.op_vault)
                    # Mirror the appended-objects loop: the cleaner turns sensitive
                    # attrs into VarRefs, so a codified secret-bearing resource
                    # must declare its variable too, or the emitted var.<name>
                    # reference has no declaration and TF_VAR warning.
                    for v in attrs.values():
                        if isinstance(v, VarRef):
                            vname = v.expr.removeprefix("var.")
                            if vname not in secret_var_names:
                                secret_var_names.append(vname)
                    rschema = _schema_for(schema, rtype)
                    new_blocks.append(render_resource(
                        rtype, slug, attrs, lifecycle=lifecycle or None,
                        block_attrs=tuple(rschema["block"].get("block_types", {}))))
                    codified.append(f"{rtype}.{slug}")
                else:
                    orphaned.append(f"{rtype}.{slug} — in state but not in committed config")

            # UI-only lifecycle: tofu can never create these (adopt in the UI,
            # then reconcile). Checked after classification above so a staged
            # deletion in this same run (added to `removed` just above) clears
            # its own violation instead of also being reported as forbidden.
            try:
                ui_lifecycle = spec_for_type(rtype).ui_lifecycle
            except KeyError:
                ui_lifecycle = False
            if ui_lifecycle and "create" in actions and f"{rtype}.{slug}" not in removed:
                forbidden.append(f"{rtype}.{slug}")

        # --- New controller objects: append resource + import block ---
        # Full-list slug assignment (reserved-seeded above) so appended slugs never
        # collide with already-managed resources.
        slug_by_key = {(t.resource_type, t.import_id): s for t, s in slug_assignment}
        new = new_targets(targets, state_identities(runner, cfg.site))
        # Stable-id guard: skip targets whose import_id is already in reconciled_new.tf.
        # Objects emitted by a prior reconcile run are never in tofu state (plan-only),
        # so new_targets always re-selects them. Without this filter, the slug space
        # grows between runs (reconciled_new.tf enters reserved), causing assign_slugs
        # to shift the same object to _2, _3, … and re-append it on every run.
        # Matching by import_id (not slug) is the stable-id principle applied to
        # reconcile's own output.
        emitted = _emitted_identities(workdir)
        new = [t for t in new if t.import_id not in emitted.get(t.resource_type, set())]
        live_new: dict[tuple[str, str], tuple[dict[str, Any], dict[str, Any]]] = {}
        for pv in plan.get("planned_values", {}).get("root_module", {}).get("resources", []):
            pslug, pattrs, plifecycle, _pv_warnings = build_resource_attrs(pv, schema, cfg.op_vault)
            live_new[(pv["type"], pslug)] = (pattrs, plifecycle)

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

        if new_blocks and not check:
            nf = workdir / "reconciled_new.tf"
            existing = nf.read_text() if nf.exists() else ""
            if existing.strip():
                prefix = existing.rstrip() + "\n\n"
            elif new_imports:
                prefix = (
                    "# Generated by ubitofu reconcile"
                    " — newly-adopted objects + their import blocks.\n"
                    "# import {} blocks are transient:"
                    " once applied they are inert and may be deleted.\n\n"
                )
            else:
                # Codified-only batch (live state-only orphans): already in
                # state, so there are no import blocks to call out.
                prefix = (
                    "# Generated by ubitofu reconcile"
                    " — state-only objects codified from live values.\n\n"
                )
            # Self-contained entries: resource block immediately followed by its
            # import block. reconcile owns this file; the operator's imports.tf
            # is never written. Codified orphans were appended to new_blocks
            # first (in the resource_changes loop, above) and carry no import —
            # already in state — so split them off before pairing the rest 1:1
            # with new_imports.
            codified_blocks = new_blocks[:len(codified)]
            appended_blocks = new_blocks[len(codified):]
            entries = list(codified_blocks) + [
                f"{block}\n\n{imp}"
                for block, imp in zip(appended_blocks, new_imports, strict=True)
            ]
            nf.write_text(prefix + "\n\n".join(entries) + "\n")
            if secret_var_names:
                write_variables_tf(workdir, secret_var_names, merge=True)
        (workdir / "tf.plan").unlink(missing_ok=True)

        # A forbidden address must not also render a contradictory "run apply"
        # pending line — forbidden already says the block must be removed or
        # adopted, never applied.
        diverged = [d for d in diverged if d[0] not in forbidden]

        # A staged deletion can leave expressions referencing the deleted
        # resource's address (e.g. an AP group listing device macs by reference);
        # merging that would fail validate. Name each dangler so the operator
        # fixes it in the same drift PR; the flag holds the attention bit.
        for addr in removed:
            pat = re.compile(rf"\b{re.escape(addr)}\b")
            for p in committed_files:
                content = staged_texts.get(p, p.read_text())
                for lineno, line in enumerate(content.splitlines(), 1):
                    if pat.search(line):
                        complex_flags.append(
                            f"{addr}: still referenced at {p.name}:{lineno} — "
                            "update before merge")

        print(format_reconcile(merged, complex_flags, appended,
                               removed=removed or None,
                               codified=codified or None,
                               secret_warnings=secret_var_names or None,
                               orphaned=orphaned or None,
                               diverged=diverged or None,
                               forbidden=forbidden or None), file=out)
        _emit_coverage(ctl, schema, workdir, res.gaps, out, check=check)
        # Outcome exit code so callers can script without grepping the report.
        # Coverage output is informational and never affects the code. Forbidden
        # takes precedence over every other outcome so the gate is unambiguous.
        if forbidden:
            return EXIT_FORBIDDEN_CREATE
        captured = bool(merged or appended or removed or codified)
        # A "pending" diverged tag is a merged-but-unapplied config change —
        # convergent for the gate (only `apply` can resolve it, so reconcile
        # must never block its own apply on one). Still reported above so the
        # operator knows apply is expected to run; just not attention-worthy.
        attention = [d for d in diverged if d[1] != "pending"]
        flagged = bool(complex_flags or attention or orphaned or secret_var_names)
        if captured and flagged:
            return EXIT_DRIFT_AND_ATTENTION
        if captured:
            return EXIT_DRIFT_CAPTURED
        if flagged:
            return EXIT_ATTENTION
        return 0
    finally:
        ctl.close()

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
    # tofu plan exited 2 = changes present. Secret attrs are sourced from vars
    # the plan cannot see into, so a diff confined to schema-sensitive attrs is
    # expected and passes; anything else is real drift -> attention required.
    if is_secrets_only_diff(plan, _sensitive_map(runner.providers_schema())):
        print("Drift: secrets-only diff (schema-sensitive attrs) — pass.", file=out)
        return 0
    print(format_drift(plan), file=out)
    return EXIT_ATTENTION
