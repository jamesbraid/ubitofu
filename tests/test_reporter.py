# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 James Braid
from ubitofu.reporter import (
    format_coverage,
    format_drift,
    format_gaps,
    is_secrets_only_diff,
)


def test_format_gaps_lists_each() -> None:
    out = format_gaps(
        [
            "2 objects found at v2/api/site/default/nat (NAT rules) "
            "with no provider resource — not imported"
        ]
    )
    assert "NAT rules" in out
    assert out.startswith("Coverage gaps")


def test_format_gaps_empty_is_clean() -> None:
    assert "no coverage gaps" in format_gaps([]).lower()


def test_format_drift_summarizes_actions() -> None:
    plan = {"resource_changes": [
        {"address": "unifi_network.lan", "change": {"actions": ["update"]}},
        {"address": "unifi_wlan.example_wlan", "change": {"actions": ["no-op"]}},
    ]}
    out = format_drift(plan)
    assert "unifi_network.lan" in out
    assert "update" in out
    assert "unifi_wlan.example_wlan" not in out  # no-op omitted


def test_secrets_only_diff_true_when_only_sensitive_attrs_change() -> None:
    plan = {"resource_changes": [{
        "type": "unifi_wlan", "address": "unifi_wlan.example_wlan",
        "change": {"actions": ["update"],
                   "before": {"passphrase": "a"}, "after": {"passphrase": "b"}}}]}
    assert is_secrets_only_diff(plan, {"unifi_wlan": {"passphrase"}}) is True


def test_secrets_only_diff_false_when_other_attr_changes() -> None:
    plan = {"resource_changes": [{
        "type": "unifi_wlan", "address": "unifi_wlan.example_wlan",
        "change": {"actions": ["update"],
                   "before": {"name": "a"}, "after": {"name": "b"}}}]}
    assert is_secrets_only_diff(plan, {"unifi_wlan": {"passphrase"}}) is False


def test_secrets_only_diff_false_on_delete() -> None:
    """Delete is a structural change, never 'secrets only'."""
    plan = {"resource_changes": [{
        "type": "unifi_wlan", "address": "unifi_wlan.example_wlan",
        "change": {"actions": ["delete"],
                   "before": {"passphrase": "secret", "name": "example_wlan"},
                   "after": {}}}]}
    assert is_secrets_only_diff(plan, {"unifi_wlan": {"passphrase"}}) is False


def test_secrets_only_diff_false_on_replace() -> None:
    """Replace (create+delete pair) is structural, never 'secrets only'."""
    plan = {"resource_changes": [{
        "type": "unifi_wlan", "address": "unifi_wlan.example_wlan",
        "change": {"actions": ["create", "delete"],
                   "before": {"passphrase": "secret"},
                   "after": {"passphrase": "newsecret"}}}]}
    assert is_secrets_only_diff(plan, {"unifi_wlan": {"passphrase"}}) is False


def test_format_secret_suppressions_lists_each_hit() -> None:
    from ubitofu.reporter import format_secret_suppressions

    out = format_secret_suppressions(
        ["unifi_network.wg: x_passphrase", "unifi_network.wg: wireguard.private_key"])
    assert "WARNING" in out
    assert "secret-shaped" in out
    assert "SECRETS rule" in out
    assert "unifi_network.wg: x_passphrase" in out
    assert "unifi_network.wg: wireguard.private_key" in out


def test_format_secret_suppressions_empty_is_empty() -> None:
    from ubitofu.reporter import format_secret_suppressions

    assert format_secret_suppressions([]) == ""


def test_format_secret_sources_lists_var_to_ref() -> None:
    from ubitofu.reporter import format_secret_sources

    out = format_secret_sources(
        {"wlan_examplenet_psk": "op://ExampleVault/unifi.wifi-psk.examplenet/password"})
    assert "var.wlan_examplenet_psk" in out
    assert "op://ExampleVault/unifi.wifi-psk.examplenet/password" in out
    assert "secret manager" in out


def test_format_secret_sources_empty_is_empty() -> None:
    from ubitofu.reporter import format_secret_sources

    assert format_secret_sources({}) == ""


def test_format_reconcile_reports_secret_var_warnings():
    from ubitofu.reporter import format_reconcile

    out = format_reconcile(merged=[], complex_flags=[], appended=["unifi_wlan.guest"],
                           secret_warnings=["wlan_guest_psk"])
    assert "wlan_guest_psk" in out
    assert "TF_VAR_wlan_guest_psk" in out


def test_format_reconcile_reports_pending_directions_together():
    from ubitofu.reporter import format_reconcile

    out = format_reconcile(merged=[], complex_flags=[], appended=[],
                           pending=[
                               ("unifi_network.example_new", "create"),
                               ("unifi_port_forward.example_old", "destroy"),
                               ("unifi_wlan.example_forgotten", "forget"),
                           ])
    assert out.count("Pending apply (config intent not yet applied):") == 1
    assert "unifi_network.example_new — create" in out
    assert "unifi_port_forward.example_old — destroy" in out
    assert "unifi_wlan.example_forgotten — forget" in out
    assert "⚠" not in out


def test_format_reconcile_reports_existence_attention():
    from ubitofu.reporter import format_reconcile

    out = format_reconcile(
        merged=[], complex_flags=[], appended=[],
        existence_attention=[
            "unifi_network.example_replace — replacement requires manual review",
            "unifi_wlan.example_orphan — state/config invariant violation",
        ],
    )
    assert "Requires attention (resource existence):" in out
    assert "replacement requires manual review" in out
    assert "state/config invariant violation" in out


def test_format_reconcile_renders_precise_deepdiff_flag():
    """Reporter must pass precise deepdiff flags through without mangling them.

    The flag string already carries path+old→new; the section header must
    appear exactly once (no double-emit of path text).
    """
    from ubitofu.reporter import format_reconcile

    flags = [
        "unifi_device.x.port_override[0].forward: 'native' → 'customize' — manual review",
    ]
    out = format_reconcile(merged=[], complex_flags=flags, appended=[])
    assert "port_override[0].forward" in out
    assert "native" in out
    assert "customize" in out
    # header appears exactly once, flag text is not repeated
    assert out.count("Flagged for manual review") == 1


def test_format_reconcile_renders_removed_section():
    from ubitofu.reporter import format_reconcile

    out = format_reconcile(merged=[], complex_flags=[], appended=[],
                           removed=["unifi_device.example_ap_2"])
    assert "Removed (deleted on controller):" in out
    assert "unifi_device.example_ap_2" in out


def test_format_reconcile_renders_imported_into_existing_config_section():
    from ubitofu.reporter import format_reconcile

    out = format_reconcile(merged=[], complex_flags=[], appended=[],
                           imported=["unifi_client.example_client"])
    assert "Imported into existing config:" in out
    assert "unifi_client.example_client" in out


def test_format_reconcile_renders_forbidden_section():
    from ubitofu.reporter import format_reconcile

    out = format_reconcile(merged=[], complex_flags=[], appended=[],
                           forbidden=["unifi_device.example_ap"])
    assert "Forbidden (device create" in out
    assert "unifi_device.example_ap" in out


def test_format_coverage_merges_gap_lines_and_accepted_count():
    out = format_coverage(["1 guest network(s) — pending",
                           "section mdns: provider lacks it"], 3)
    assert "Coverage gaps:" in out
    assert "  - 1 guest network(s) — pending" in out
    assert "3 accepted item(s)" in out and "COVERAGE.md" in out


def test_format_coverage_clean():
    assert format_coverage([], 0) == "Coverage: no coverage gaps detected."
