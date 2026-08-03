# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 James Braid
import json
import os
import re
import tempfile
from contextlib import ExitStack
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import IO, Any

from deepdiff import DeepDiff

from .cleaner import VarRef, clean_resource, normalize_emitted, strip_secret_shaped
from .config import Config
from .controller import Controller, controller_from_config
from .coverage import audit, write_coverage_md
from .enumerator import ImportTarget, derive_identity, enumerate_controller
from .hcl_surgeon import (
    declared_attrs,
    delete_resource_block,
    find_resource_block_span,
    insert_scalar,
    update_scalar,
)
from .hcl_writer import render_resource, render_variables
from .import_emitter import assign_slugs, emit_import_blocks
from .manifest import spec_for_type
from .reporter import (
    format_coverage,
    format_drift,
    format_migrate,
    format_reconcile,
    format_secret_sources,
    format_secret_suppressions,
    is_secrets_only_diff,
)
from .schema_diff import (
    diff_resources,
    filter_to_config,
    lock_versions,
    reduce_schema,
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


class ExistenceDecision(StrEnum):
    """The mutation or report outcome for one resource address."""

    MANAGED = "managed"
    IMPORT_EXISTING_CONFIG = "import-existing-config"
    PENDING_CREATE = "pending-create"
    PENDING_DESTROY = "pending-destroy"
    PENDING_FORGET = "pending-forget"
    FORBIDDEN_CREATE = "forbidden-create"
    CONTROLLER_DELETED = "controller-deleted"
    APPEND_NEW = "append-new"
    REPLACEMENT_ATTENTION = "replacement-attention"
    IDENTITY_ATTENTION = "identity-attention"
    EXPANDED_DELETION_ATTENTION = "expanded-deletion-attention"
    INVARIANT_ATTENTION = "invariant-attention"


@dataclass(frozen=True)
class ExistenceFacts:
    """Immutable existence snapshot for one full plan/state address."""

    address: str
    resource_type: str
    config_present: bool
    state_present: bool
    config_block_direct: bool = True
    actions: tuple[str, ...] = ()
    action_reason: str | None = None
    live_identity: str | None = None
    live_present: bool = False
    live_type_present: bool = False
    identity_joinable: bool = True
    scratch_import: bool = False
    existing_import: bool = False
    ui_lifecycle: bool = False
    append_suppressed: bool = False


@dataclass(frozen=True)
class ExistenceClassification:
    """Pure classifier output; mutation consumes this in a later phase."""

    address: str
    kind: ExistenceDecision
    import_id: str | None = None


def classify_existence(facts: ExistenceFacts) -> ExistenceClassification:
    """Classify desired and current existence without reading or writing files."""
    if facts.scratch_import:
        if facts.live_present and not facts.append_suppressed:
            kind = ExistenceDecision.APPEND_NEW
        else:
            kind = ExistenceDecision.MANAGED
    elif facts.actions in (("delete", "create"), ("create", "delete")):
        kind = ExistenceDecision.REPLACEMENT_ATTENTION
    elif facts.config_present and facts.state_present:
        if facts.actions == ("create",):
            if facts.identity_joinable and not facts.live_present:
                kind = (
                    ExistenceDecision.CONTROLLER_DELETED
                    if facts.config_block_direct
                    else ExistenceDecision.EXPANDED_DELETION_ATTENTION
                )
            elif facts.live_present:
                kind = ExistenceDecision.PENDING_CREATE
            else:
                kind = ExistenceDecision.INVARIANT_ATTENTION
        elif facts.actions in (("delete",), ("forget",)):
            kind = ExistenceDecision.INVARIANT_ATTENTION
        else:
            kind = ExistenceDecision.MANAGED
    elif facts.config_present:
        if not facts.identity_joinable and (facts.live_present or facts.live_type_present):
            kind = ExistenceDecision.IDENTITY_ATTENTION
        elif facts.existing_import and facts.live_present:
            kind = ExistenceDecision.MANAGED
        elif facts.live_present and facts.live_identity is not None:
            kind = ExistenceDecision.IMPORT_EXISTING_CONFIG
        elif facts.ui_lifecycle:
            kind = ExistenceDecision.FORBIDDEN_CREATE
        else:
            kind = ExistenceDecision.PENDING_CREATE
    elif facts.state_present:
        if facts.actions == ("forget",):
            kind = ExistenceDecision.PENDING_FORGET
        elif (
            facts.actions == ("delete",)
            and facts.action_reason == "delete_because_no_resource_config"
        ):
            kind = ExistenceDecision.PENDING_DESTROY
        else:
            kind = ExistenceDecision.INVARIANT_ATTENTION
    elif facts.live_present:
        kind = ExistenceDecision.APPEND_NEW
    else:
        kind = ExistenceDecision.MANAGED
    return ExistenceClassification(facts.address, kind, facts.live_identity)


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
            if p.name not in _RECONCILE_SCAFFOLD
            and not p.name.startswith("ubitofu-reconcile-")]


# Where the last-seen provider schema is kept, next to the config it describes.
BASELINE_PATH = Path(".ubitofu") / "provider-baseline.json"


def run_migrate(cfg: Config, out: IO[str], *, write_baseline: bool = False) -> int:
    """Report what a provider bump breaks, before anything tries to plan.

    Reads the installed provider's schema and the committed HCL, and nothing
    else — the controller is never contacted. It writes nothing either. The
    attributes it reports as removed are often nested (radio_table.*) and the
    surgeon edits only top-level scalars, so it names the file:line an
    operator must change and stops there.
    """
    workdir = Path(cfg.workdir)
    runner = TofuRunner(workdir=workdir)
    current = reduce_schema(runner.providers_schema())
    lock = workdir / ".terraform.lock.hcl"
    versions = lock_versions(lock.read_text()) if lock.exists() else {}
    baseline_file = workdir / BASELINE_PATH

    if write_baseline or not baseline_file.exists():
        baseline_file.parent.mkdir(parents=True, exist_ok=True)
        baseline_file.write_text(json.dumps(
            {"providers": versions, "resources": current}, indent=2, sort_keys=True))
        why = "refreshed" if write_baseline else "no baseline yet — recorded"
        print(f"Provider migration: {why} {baseline_file}. "
              "Re-run after the provider bump to see what it changes.", file=out)
        return 0

    baseline = json.loads(baseline_file.read_text())
    findings = diff_resources(baseline.get("resources", {}), current)
    texts = {p.name: p.read_text() for p in _committed_tf_files(workdir)}
    findings = filter_to_config(findings, texts)
    print(format_migrate(findings, baseline.get("providers", {}), versions), file=out)
    return 11 if findings else 0


def _find_file_for(files: list[Path], rtype: str, slug: str) -> Path | None:
    """Return the committed file whose text declares resource TYPE.SLUG, if any."""
    for p in files:
        if find_resource_block_span(p.read_text(), rtype, slug) is not None:
            return p
    return None


def _is_scalar(v: object) -> bool:
    # bool is an int subclass — allowed; VarRef (secret ref) and containers are not.
    return isinstance(v, str | int | float | bool)


def _unknown_at(unknown: object, segments: list[str | int]) -> bool:
    """True when the plan marks this path (or an ancestor) as unknown.

    tofu's ``after_unknown`` mirrors ``after``: ``true`` at an unknown leaf, and
    ``true`` for a whole subtree that is unknown wholesale. An unknown value is
    a reference this apply will resolve, so it is pending — never drift.
    """
    node = unknown
    for seg in segments:
        if node is True:
            return True          # ancestor unknown wholesale
        if isinstance(seg, int):
            if not isinstance(node, list) or seg >= len(node):
                return False
            node = node[seg]
        else:
            if not isinstance(node, dict) or seg not in node:
                return False
            node = node[seg]
    return node is True


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
    unknown: dict[str, Any] | None = None,
) -> list[str]:
    """Return precise flag strings for drift that reconcile cannot auto-edit.

    Walks live/committed attr dicts:
    - Absent/added attrs → manual add/remove flag.
    - Scalar diffs → silently skipped (handled by update_scalar in _diff_resource).
    - Non-scalar diffs → expanded by DeepDiff into per-path old→new strings, e.g.
      ``unifi_device.x.port_override[0].forward: 'native' → 'customize' — manual review``.

    ``live`` is the controller state, ``committed`` is what's in HCL.
    ``addr`` is the resource address prefix, e.g. ``unifi_device.x``.

    ``unknown`` is the plan's ``after_unknown`` for this resource. A value that
    references a resource the same apply creates is unknown at plan time: tofu
    writes null into ``after`` and records the path here, and ``is_empty`` then
    drops the null, so the attribute reads as absent-from-config and would be
    flagged as drift reconcile cannot capture. It is pending an apply, not
    drift — flagging it deadlocked the apply gate, which blocks on exactly that
    signal and can never clear it. Paths marked unknown are skipped; everything
    else, including a real diff beside an unknown sibling, still flags.
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
        if unknown is not None and _unknown_at(unknown, [attr]):
            continue  # whole attr unknown at plan time — pending, not drift
        if lv is _MISSING or cv is _MISSING:
            where = "absent on controller" if lv is _MISSING else "added on controller"
            flags.append(f"{full_addr}: {where} — manual add/remove")
            continue
        if _is_scalar(lv) and _is_scalar(cv):
            continue  # scalar: handled by update_scalar, not flagged here
        try:
            # Tree view so each change carries its path as a list of segments;
            # the unknown lookup needs structure, and re-parsing the string form
            # would just be undoing deepdiff's own flattening.
            diff = DeepDiff(cv, lv, verbose_level=2, view="tree")
            if not diff:
                # Values compare equal under deepdiff despite differing under ==
                # (e.g. type coercions) — fall back to a generic flag.
                flags.append(f"{full_addr}: nested/list/map drift — manual review")
                continue
            for change_type, levels in diff.items():
                for level in levels:
                    # Paths are relative to this attr's value, so the lookup into
                    # after_unknown (which mirrors the whole `after` object) has
                    # to be prefixed with the attr itself.
                    if unknown is not None and _unknown_at(
                            unknown, [attr, *level.path(output_format="list")]):
                        continue
                    change_val = (
                        level.t1 if change_type.endswith("_removed") else level.t2
                    )
                    friendly = _friendly_deepdiff_path(level.path())
                    # Friendly may start with '[' (integer index at root) or be empty
                    if not friendly:
                        full_path = full_addr
                    elif friendly.startswith("["):
                        full_path = f"{full_addr}{friendly}"
                    else:
                        full_path = f"{full_addr}.{friendly}"
                    if change_type == "values_changed":
                        # Tree view carries old/new on the level itself; the dict
                        # view's {"old_value","new_value"} payload is not present.
                        flags.append(
                            f"{full_path}: {level.t1!r} → {level.t2!r} — manual review"
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
    unknown: dict[str, Any] | None = None,
    source_text: str | None = None,
) -> str:
    """Merge scalar drift into *path* in place; flag everything else.

    ``live`` and ``committed`` are both cleaned attr dicts (build_resource_attrs
    over change.before / change.after), so sensitive values are already VarRefs
    or suppressed and can never diff as plaintext.

    Scalar diffs go through update_scalar (comment-preserving surgeon).
    All other drift — absent/added attrs, nested blocks, lists, maps — is
    handed to reconcile_complex_flags which uses DeepDiff to produce precise
    per-path old→new flag strings.

    ``committed`` is the PLANNED value. It says what apply will write, not
    what the operator asked for: an attribute the config never mentions still
    appears there, carrying the default the provider gave it. Only the
    committed text tells the two apart. An attribute the block does not
    declare therefore skips the three-way comparison — there is no intent to
    keep, and no committed literal to anchor an edit on — and insert_scalar
    writes the live value into the block instead. Without that, a provider
    that starts to default an attribute looks like deliberate config intent,
    reconcile reports nothing, and apply turns the setting off. Ubiquiti
    0.101.0 did exactly this to
    unifi_wlan.roaming_assistant_na_enabled with a static ``false``.

    ``state_attrs``, when given, is the last-applied snapshot (also a cleaned
    attr dict, via build_resource_attrs over the tofu state row) and turns
    each DECLARED scalar comparison three-way: the state shows the difference
    between drift (live moved, state==committed) and unapplied config intent
    (committed moved, live==state — leave it for `apply`, never revert it),
    and flags real conflicts (all three differ) instead of guessing. ``None``
    preserves the old two-way behavior; an attr absent from ``state_attrs``
    (e.g. legacy state rows carrying only ``{"id": ...}``) falls back to it too.

    ``check``, when true, still classifies every scalar merge into ``merged``
    but skips the ``path.write_text`` — the apply gate's dry run.
    """
    text = path.read_text() if source_text is None else source_text
    declared = declared_attrs(text, rtype, slug)
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
        if attr not in declared:
            # The block says nothing about this attribute, so ``cv`` is the
            # provider's default rather than the operator's intent. There is
            # no committed value to keep and none to anchor an edit on. Write
            # the live value in, or apply writes the default over it.
            try:
                text = insert_scalar(text, rtype, slug, attr, lv)
            except (LookupError, ValueError) as exc:
                complex_flags.append(f"{addr}: could not codify in place ({exc})")
                continue
            merged.append(f"{addr}: absent -> {lv!r} (provider default {cv!r})")
            changed = True
            continue
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
    complex_flags.extend(
        reconcile_complex_flags(live, committed, f"{rtype}.{slug}", unknown=unknown))
    if changed and not check and source_text is None:
        path.write_text(text)
    return text


def _import_block(rtype: str, slug: str, import_id: str) -> str:
    return _import_block_to(f"{rtype}.{slug}", import_id)


def _import_block_to(address: str, import_id: str) -> str:
    return f'import {{\n  to = {address}\n  id = "{import_id}"\n}}'


_RESOURCE_HDR_RE = re.compile(r'^resource\s+"([^"]+)"\s+"([^"]+)"', re.MULTILINE)
_MODULE_HDR_RE = re.compile(r'^module\s+"([^"]+)"', re.MULTILINE)
_INSTANCE_KEY_RE = re.compile(r'\[(?:"(?:\\.|[^"])*"|[^]]+)\]')


def _committed_addresses(committed_files: list[Path]) -> set[str]:
    """Return 'type.slug' for every resource block present in committed *.tf files."""
    addrs: set[str] = set()
    for p in committed_files:
        for m in _RESOURCE_HDR_RE.finditer(p.read_text()):
            addrs.add(f"{m.group(1)}.{m.group(2)}")
    return addrs


def _configured_addresses(workdir: Path) -> set[str]:
    """Snapshot addresses declared by operator or persisted generated config."""
    return _committed_addresses(_committed_tf_files(workdir))


def _committed_module_calls(committed_files: list[Path]) -> set[str]:
    """Return root module call names declared by committed configuration."""
    return {
        match.group(1)
        for path in committed_files
        for match in _MODULE_HDR_RE.finditer(path.read_text())
    }


def _config_covers_address(
    address: str,
    configured: set[str],
    module_calls: set[str] | frozenset[str],
    actions: tuple[str, ...],
) -> bool:
    """Map expanded plan addresses back to their committed HCL declaration."""
    if address in configured:
        return True
    # A deleted/forgotten instance is absent from desired config even when its
    # resource or ancestor module declaration still exists (count/for_each and
    # child-module reductions are the common cases).
    if actions in (("delete",), ("forget",)):
        return False
    unkeyed = _INSTANCE_KEY_RE.sub("", address)
    if unkeyed.startswith("module."):
        parts = unkeyed.split(".")
        return len(parts) > 1 and parts[1] in module_calls
    return unkeyed in configured


def _row_address(row: dict[str, Any]) -> str:
    """Use tofu's full address, with type.name fallback for legacy fixtures."""
    return str(row.get("address") or f"{row['type']}.{row['name']}")


def _is_managed(row: dict[str, Any]) -> bool:
    """True for a managed resource, false for a data source.

    Data sources are read, never created or destroyed, so they have no
    existence for reconcile to decide about. Left in, one arrives in state and
    in the plan carrying neither a create nor a delete, which reads as a
    state/config invariant violation and raises attention every run.

    Absent `mode` means managed: tofu always writes it, but the legacy
    fixtures predate it.
    """
    return bool(row.get("mode", "managed") != "data")


def _module_resources(module: dict[str, Any]) -> list[dict[str, Any]]:
    resources = [r for r in module.get("resources", []) if _is_managed(r)]
    for child in module.get("child_modules", []):
        resources.extend(_module_resources(child))
    return resources


def _state_rows_by_address(state: dict[str, Any]) -> dict[str, dict[str, Any]]:
    root = state.get("values", {}).get("root_module", {})
    return {_row_address(row): row for row in _module_resources(root)}


def _plan_changes_by_address(plan: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {_row_address(row): row
            for row in plan.get("resource_changes", []) if _is_managed(row)}


# Matches the import block format produced by _import_block:
#   import {
#     to = TYPE.slug
#     id = "IMPORT_ID"
#   }
_EMITTED_IMPORT_RE = re.compile(
    r'import\s*\{\s*to\s*=\s*([^\r\n]+?)\s*\r?\n\s*id\s*=\s*"([^"]+)"\s*\}',
    re.DOTALL,
)


def _emitted_imports(workdir: Path) -> dict[str, str]:
    """Return every persisted import keyed by its original address."""
    imports: dict[str, str] = {}
    for path in _committed_tf_files(workdir):
        imports.update({
            match.group(1): match.group(2)
            for match in _EMITTED_IMPORT_RE.finditer(path.read_text())
        })
    return imports


def _state_identity_maps(
    state_rows: dict[str, dict[str, Any]], site: str
) -> tuple[dict[str, str], dict[str, set[str]]]:
    by_address: dict[str, str] = {}
    by_type: dict[str, set[str]] = {}
    for address, row in state_rows.items():
        rtype = row["type"]
        try:
            ident = _identity(spec_for_type(rtype).id_rule, row.get("values", {}), site)
        except KeyError:
            continue
        if ident is not None:
            by_address[address] = ident
            by_type.setdefault(rtype, set()).add(ident)
    return by_address, by_type


def _planned_values_by_address(plan: dict[str, Any]) -> dict[str, dict[str, Any]]:
    root = plan.get("planned_values", {}).get("root_module", {})
    return {_row_address(row): row for row in _module_resources(root)}


def _existence_facts(
    *,
    configured: set[str],
    state_rows: dict[str, dict[str, Any]],
    plan_changes: dict[str, dict[str, Any]],
    targets: list[ImportTarget],
    scratch_by_address: dict[str, ImportTarget],
    emitted_imports: dict[str, str],
    site: str,
    module_calls: set[str] | frozenset[str] = frozenset(),
) -> dict[str, ExistenceFacts]:
    """Join immutable config/state/plan/live snapshots by address and identity."""
    live_by_type: dict[str, set[str]] = {}
    for target in targets:
        live_by_type.setdefault(target.resource_type, set()).add(target.import_id)
    untracked_live_types = {
        target.resource_type for target in scratch_by_address.values()
    }
    state_identity, _ = _state_identity_maps(state_rows, site)
    state_owner = {
        (row["type"], ident): address
        for address, ident in state_identity.items()
        if (row := state_rows.get(address)) is not None
    }

    addresses = configured | set(state_rows) | set(plan_changes) | set(scratch_by_address)
    config_present_by_address: dict[str, bool] = {}
    for address in addresses:
        rc = plan_changes.get(address, {})
        actions = tuple(rc.get("change", {}).get("actions") or ())
        config_present_by_address[address] = _config_covers_address(
            address, configured, module_calls, actions)

    identity_by_config: dict[str, str] = {}
    type_by_config: dict[str, str] = {}
    ambiguous_types: set[str] = set()
    for address, config_present in config_present_by_address.items():
        if not config_present:
            continue
        rc = plan_changes.get(address, {})
        state_row = state_rows.get(address, {})
        rtype = rc.get("type") or state_row.get("type")
        if rtype is not None:
            type_by_config[address] = rtype
        if address in state_rows:
            ident = state_identity.get(address)
        elif address in emitted_imports:
            ident = emitted_imports[address]
        else:
            values = rc.get("change", {}).get("after") or {}
            ident = None
            if rtype is not None:
                try:
                    ident = _identity(spec_for_type(rtype).id_rule, values, site)
                except KeyError:
                    pass
        if ident is not None:
            identity_by_config[address] = ident
        elif address not in state_rows:
            if rtype is not None and rtype in untracked_live_types:
                ambiguous_types.add(rtype)

    config_claims: dict[tuple[str, str], list[str]] = {}
    for address, ident in identity_by_config.items():
        rtype = type_by_config.get(address)
        if rtype is not None:
            config_claims.setdefault((rtype, ident), []).append(address)
    conflicted_config: set[str] = set()
    blocked_live: set[tuple[str, str]] = set()
    claimed_live: set[tuple[str, str]] = set()
    for key, claimants in config_claims.items():
        owner = state_owner.get(key)
        conflicts = len(claimants) > 1 or any(
            owner is not None and owner != address for address in claimants
        )
        if conflicts:
            conflicted_config.update(claimants)
            blocked_live.add(key)
        elif key[1] in live_by_type.get(key[0], set()):
            claimed_live.add(key)

    facts: dict[str, ExistenceFacts] = {}
    for address in sorted(addresses):
        rc = plan_changes.get(address, {})
        state_row = state_rows.get(address, {})
        scratch_target = scratch_by_address.get(address)
        rtype = rc.get("type") or state_row.get("type")
        if rtype is None and scratch_target is not None:
            rtype = scratch_target.resource_type
        if rtype is None:
            continue
        change = rc.get("change", {})
        actions = tuple(change.get("actions") or ())
        ident = (
            scratch_target.import_id
            if scratch_target is not None
            else identity_by_config.get(address) or state_identity.get(address)
        )
        live_present = ident is not None and ident in live_by_type.get(rtype, set())
        config_present = config_present_by_address[address]
        state_present = address in state_rows
        joinable = (
            address not in conflicted_config
            and (ident is not None or rtype not in untracked_live_types)
        )
        try:
            ui_lifecycle = spec_for_type(rtype).ui_lifecycle
        except KeyError:
            ui_lifecycle = False
        facts[address] = ExistenceFacts(
            address=address,
            resource_type=rtype,
            config_present=config_present,
            state_present=state_present,
            config_block_direct=address in configured,
            actions=actions,
            action_reason=rc.get("action_reason"),
            live_identity=ident if live_present else None,
            live_present=live_present,
            live_type_present=(
                rtype in untracked_live_types or address in conflicted_config
            ),
            identity_joinable=joinable,
            scratch_import=scratch_target is not None,
            existing_import=address in emitted_imports,
            ui_lifecycle=ui_lifecycle,
            append_suppressed=(
                scratch_target is not None
                and ((rtype, scratch_target.import_id) in claimed_live
                     or (rtype, scratch_target.import_id) in blocked_live
                     or rtype in ambiguous_types)
            ),
        )
    return facts


def run_reconcile(cfg: Config, out: IO[str], check: bool = False) -> int:
    """Classify desired/current existence, then stage and apply safe mutations."""
    ctl = controller_from_config(cfg)
    try:
        res = enumerate_controller(ctl)
        workdir = Path(cfg.workdir)
        runner = TofuRunner(workdir=workdir)
        targets = res.targets

        # Immutable pre-scratch snapshots. Persisted generated resource files are
        # config; reconcile scratch and generated stubs are not operator intent.
        committed_files = _committed_tf_files(workdir)
        configured = _committed_addresses(committed_files)
        module_calls = _committed_module_calls(committed_files)
        state = runner.show_state_json()
        state_rows = _state_rows_by_address(state)
        state_identity, managed_by_type = _state_identity_maps(state_rows, cfg.site)
        emitted_imports = _emitted_imports(workdir)
        emitted_keys = {
            (address.rsplit(".", 2)[-2], import_id)
            for address, import_id in emitted_imports.items()
        }

        # Existing state/imports need no scratch import. Every remaining live
        # target gets an explicit scratch address that can never be mistaken for
        # committed intent during classification.
        scratch_targets = [
            target for target in targets
            if target.import_id not in managed_by_type.get(target.resource_type, set())
            and (target.resource_type, target.import_id) not in emitted_keys
        ]
        reserved = configured | set(state_rows)
        slug_assignment = assign_slugs(scratch_targets, reserved=reserved)
        scratch_by_address = {
            f"{target.resource_type}.{slug}": target
            for target, slug in slug_assignment
        }

        with ExitStack() as cleanup:
            scratch_fd, scratch_name = tempfile.mkstemp(
                dir=workdir, prefix="ubitofu-reconcile-", suffix=".tf")
            scratch = Path(scratch_name)
            cleanup.callback(scratch.unlink, missing_ok=True)
            os.close(scratch_fd)
            stub_fd, stub_name = tempfile.mkstemp(
                dir=workdir, prefix="ubitofu-reconcile-generated-", suffix=".tf")
            generated_stub = Path(stub_name)
            cleanup.callback(generated_stub.unlink, missing_ok=True)
            os.close(stub_fd)
            plan_fd, plan_name = tempfile.mkstemp(
                dir=workdir, prefix=".ubitofu-reconcile-plan-")
            plan_file = Path(plan_name)
            cleanup.callback(plan_file.unlink, missing_ok=True)
            os.close(plan_fd)
            scratch.write_text(
                "\n\n".join(_import_block(t.resource_type, s, t.import_id)
                            for t, s in slug_assignment) + "\n"
            )
            generated_stub.unlink()
            plan_file.unlink()
            if slug_assignment:
                runner.plan(out=plan_file, generate_config_out=generated_stub)
            else:
                runner.plan(out=plan_file)
            schema = runner.providers_schema()
            plan = runner.show_json(plan_file)

        plan_changes = _plan_changes_by_address(plan)
        facts = _existence_facts(
            configured=configured,
            state_rows=state_rows,
            plan_changes=plan_changes,
            targets=targets,
            scratch_by_address=scratch_by_address,
            emitted_imports=emitted_imports,
            site=cfg.site,
            module_calls=module_calls,
        )
        decisions = {
            address: classify_existence(address_facts)
            for address, address_facts in facts.items()
        }

        merged: list[str] = []
        complex_flags: list[str] = []
        appended: list[str] = []
        removed: list[str] = []
        imported: list[str] = []
        pending: list[tuple[str, str]] = []
        existence_attention: list[str] = []
        forbidden: list[str] = []
        staged_texts: dict[Path, str] = {}
        import_only_entries: list[str] = []
        resource_import_entries: list[str] = []
        secret_var_names: list[str] = []

        planned_values = _planned_values_by_address(plan)
        for address, decision in decisions.items():
            address_facts = facts[address]
            rtype = address_facts.resource_type
            rc = plan_changes.get(address, {})
            slug = rc.get("name") or state_rows.get(address, {}).get("name")
            if slug is None:
                slug = address.rsplit(".", 1)[-1]
            path = _find_file_for(committed_files, rtype, slug)

            if decision.kind is ExistenceDecision.PENDING_CREATE:
                pending.append((address, "create"))
            elif decision.kind is ExistenceDecision.PENDING_DESTROY:
                pending.append((address, "destroy"))
            elif decision.kind is ExistenceDecision.PENDING_FORGET:
                pending.append((address, "forget"))
            elif decision.kind is ExistenceDecision.FORBIDDEN_CREATE:
                forbidden.append(address)
            elif decision.kind is ExistenceDecision.REPLACEMENT_ATTENTION:
                existence_attention.append(
                    f"{address} — replacement requires manual review")
            elif decision.kind is ExistenceDecision.IDENTITY_ATTENTION:
                existence_attention.append(
                    f"{address} — identity cannot safely match configured and live "
                    f"objects; automatic {rtype} append suppressed — manual review")
            elif decision.kind is ExistenceDecision.EXPANDED_DELETION_ATTENTION:
                existence_attention.append(
                    f"{address} — controller deletion belongs to an expanded config "
                    "address; removing its committed block would also remove sibling "
                    "instances — manual review")
            elif decision.kind is ExistenceDecision.INVARIANT_ATTENTION:
                reason = address_facts.action_reason or "no supported removal reason"
                existence_attention.append(
                    f"{address} — state/config invariant violation ({reason})")
            elif decision.kind is ExistenceDecision.CONTROLLER_DELETED:
                if path is None:
                    existence_attention.append(
                        f"{address} — controller deletion has no committed block to remove")
                else:
                    text = staged_texts.get(path, path.read_text())
                    staged_texts[path] = delete_resource_block(text, rtype, slug)
                    removed.append(address)
            elif decision.kind is ExistenceDecision.IMPORT_EXISTING_CONFIG:
                if decision.import_id is None:
                    existence_attention.append(
                        f"{address} — matched live object has no safe import identity")
                else:
                    import_only_entries.append(
                        _import_block_to(decision.address, decision.import_id))
                    imported.append(address)
            elif decision.kind is ExistenceDecision.APPEND_NEW:
                target = scratch_by_address.get(address)
                pv = planned_values.get(address)
                if target is None or pv is None:
                    complex_flags.append(
                        f"{address} — new object but no planned values; run generate")
                    continue
                _, attrs, lifecycle, _warnings = build_resource_attrs(
                    pv, schema, cfg.op_vault)
                for value in attrs.values():
                    if isinstance(value, VarRef):
                        name = value.expr.removeprefix("var.")
                        if name not in secret_var_names:
                            secret_var_names.append(name)
                rschema = _schema_for(schema, rtype)
                block = render_resource(
                    rtype, slug, attrs, lifecycle=lifecycle or None,
                    block_attrs=tuple(rschema["block"].get("block_types", {})))
                resource_import_entries.append(
                    f"{block}\n\n{_import_block(rtype, slug, target.import_id)}")
                appended.append(f"{address} ({target.import_id})")

            # Attribute drift remains separate from existence. It is staged in
            # memory so a later forbidden decision can return 13 byte-identically.
            if (
                address_facts.config_present
                and rc.get("change", {}).get("actions") == ["update"]
                and path is not None
            ):
                change = rc["change"]
                _, live_attrs, _, _ = build_resource_attrs(
                    {"type": rtype, "name": slug,
                     "values": change.get("before") or {}},
                    schema, cfg.op_vault)
                _, committed_attrs, _, _ = build_resource_attrs(
                    {"type": rtype, "name": slug,
                     "values": change.get("after") or {}},
                    schema, cfg.op_vault)
                state_attrs = None
                state_row = state_rows.get(address)
                if state_row is not None:
                    _, state_attrs, _, _ = build_resource_attrs(
                        {"type": rtype, "name": slug,
                         "values": state_row.get("values", {})},
                        schema, cfg.op_vault)
                source = staged_texts.get(path, path.read_text())
                staged_texts[path] = _diff_resource(
                    rtype, slug, live_attrs, committed_attrs, path,
                    merged, complex_flags, state_attrs=state_attrs,
                    check=True, unknown=change.get("after_unknown") or None,
                    source_text=source)

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

        generated_entries = import_only_entries + resource_import_entries
        generated_text: str | None = None
        if generated_entries:
            nf = workdir / "reconciled_new.tf"
            existing = nf.read_text() if nf.exists() else ""
            prefix = existing.rstrip() + "\n\n" if existing.strip() else (
                "# Generated by ubitofu reconcile"
                " — imports for existing config and newly discovered objects.\n\n"
            )
            generated_text = prefix + "\n\n".join(generated_entries) + "\n"

        print(format_reconcile(merged, complex_flags, appended,
                               removed=removed or None,
                               secret_warnings=secret_var_names or None,
                               forbidden=forbidden or None,
                               imported=imported or None,
                               pending=pending or None,
                               existence_attention=existence_attention or None), file=out)

        # Forbidden takes precedence and forbids every persistent mutation.
        if not check and not forbidden:
            for path, text in staged_texts.items():
                if path.read_text() != text:
                    path.write_text(text)
            if generated_text is not None:
                (workdir / "reconciled_new.tf").write_text(generated_text)
            if secret_var_names:
                write_variables_tf(workdir, secret_var_names, merge=True)
        _emit_coverage(ctl, schema, workdir, res.gaps, out,
                       check=check or bool(forbidden))

        # Outcome exit code so callers can script without grepping the report.
        # Coverage output is informational and never affects the code. Forbidden
        # takes precedence over every other outcome so the gate is unambiguous.
        if forbidden:
            return EXIT_FORBIDDEN_CREATE
        captured = bool(merged or appended or removed or imported)
        flagged = bool(complex_flags or existence_attention or secret_var_names)
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
