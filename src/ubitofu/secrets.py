# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 James Braid
"""Sensitive-attribute table and op:// reference helpers.

Resolves VarRef markers (produced by the cleaner) into concrete variable names
and provides lifecycle { ignore_changes } data for resources with write-only attrs.

Config-driven vault: op:// references are rendered at call-time with a vault
string that callers supply from config — no homelab-specific values are
hardcoded here.

Nested-block secrets decision
------------------------------
The UniFi Terraform provider schema does not mark any block_types attribute as
sensitive or write_only for the resource types in scope (unifi_wlan,
unifi_network, unifi_device, unifi_port_forward, unifi_firewall_rule, etc.).
All sensitive attrs (passphrase on unifi_wlan) are top-level, handled by the
SECRETS table below.  WireGuard private keys are returned as null by the
provider read and produce no plan diff, so they are intentionally left
unmanaged (no rule).  If a future provider version adds a sensitive block attr,
add a rule to SECRETS with a nested attr path; for now the cleaner's
block_types recursion correctly omits sensitive=True top-level fields and there
is nothing to propagate further.
"""

from dataclasses import dataclass, field

from .cleaner import VarRef


@dataclass(frozen=True)
class SecretRule:
    resource_type: str
    attr: str
    var_template: str
    op_template: str
    # Attrs that the provider writes internally (write_only) and that Tofu
    # should ignore in lifecycle comparisons.
    lifecycle_ignore: tuple[str, ...] = field(default_factory=tuple)


# Table of known sensitive attributes.
# op_template uses {vault} (config-supplied) and {name} (the resource slug).
# var_template uses {name} only.
SECRETS: tuple[SecretRule, ...] = (
    SecretRule(
        "unifi_wlan",
        "passphrase",
        "wlan_{name}_psk",
        "op://{vault}/unifi.wifi-psk.{name}/password",
        lifecycle_ignore=("passphrase_wo",),
    ),
    SecretRule(
        "unifi_dynamic_dns",
        "password",
        "dynamic_dns_{name}_password",
        "op://{vault}/dynamic-dns.{name}/password",
    ),
)


def sensitive_attrs(resource_schema: dict) -> set[str]:  # type: ignore[type-arg]
    """Return attr names flagged sensitive or write_only in the provider schema."""
    attrs = resource_schema["block"]["attributes"]
    return {n for n, a in attrs.items() if a.get("sensitive") or a.get("write_only")}


def var_name(rule: SecretRule, context: dict) -> str:  # type: ignore[type-arg]
    """Render the Terraform variable name for a secret rule."""
    return rule.var_template.format(**context)


def op_reference(rule: SecretRule, context: dict, vault: str) -> str:  # type: ignore[type-arg]
    """Render the op:// secret reference.  vault comes from caller config."""
    return rule.op_template.format(vault=vault, **context)


def secret_sources(
    resource_type: str,
    slug: str,
    resource_schema: dict,  # type: ignore[type-arg]
    vault: str,
) -> dict[str, str]:
    """Map each matched SECRETS rule to {var name: op:// reference}.

    Mirrors resolve_secrets' matching; used to declare variables and to
    report where the operator's secret-manager values should come from.
    """
    present = sensitive_attrs(resource_schema)
    ctx = {"name": slug}
    return {
        var_name(rule, ctx): op_reference(rule, ctx, vault)
        for rule in SECRETS
        if rule.resource_type == resource_type and rule.attr in present
    }


def resolve_secrets(
    resource_type: str,
    slug: str,
    resource_schema: dict,  # type: ignore[type-arg]
) -> tuple[dict[str, VarRef], dict, set[str]]:  # type: ignore[type-arg]
    """Match SECRETS rules against the schema and return (refs, lifecycle, suppress).

    refs     — maps attr name → VarRef("var.<rendered_name>")
    lifecycle — {"ignore_changes": [...]} or {} when nothing to ignore
    suppress — sensitive attrs with no SECRETS rule; must be omitted from HCL
               and are already listed in lifecycle["ignore_changes"] so tofu
               never plans a wipe of the controller-side value.
    """
    refs: dict[str, VarRef] = {}
    lifecycle: dict[str, list[str]] = {}
    present = sensitive_attrs(resource_schema)
    ctx = {"name": slug}
    ruled: set[str] = set()
    for rule in SECRETS:
        if rule.resource_type == resource_type and rule.attr in present:
            refs[rule.attr] = VarRef(f"var.{var_name(rule, ctx)}")
            if rule.lifecycle_ignore:
                lifecycle.setdefault("ignore_changes", []).extend(rule.lifecycle_ignore)
            ruled.add(rule.attr)

    # Sensitive attrs with no SECRETS rule: suppress from HCL and add to
    # ignore_changes so tofu never plans a wipe of the controller-side value.
    suppress = present - ruled
    for attr in sorted(suppress):  # sorted for determinism
        lifecycle.setdefault("ignore_changes", []).append(attr)

    return refs, lifecycle, suppress
