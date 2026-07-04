# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 James Braid
from ubitofu.reporter import (
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
        {"address": "unifi_wlan.iot", "change": {"actions": ["no-op"]}},
    ]}
    out = format_drift(plan)
    assert "unifi_network.lan" in out
    assert "update" in out
    assert "unifi_wlan.iot" not in out  # no-op omitted


def test_secrets_only_diff_true_when_only_sensitive_attrs_change() -> None:
    plan = {"resource_changes": [{
        "type": "unifi_wlan", "address": "unifi_wlan.iot",
        "change": {"actions": ["update"],
                   "before": {"passphrase": "a"}, "after": {"passphrase": "b"}}}]}
    assert is_secrets_only_diff(plan, {"unifi_wlan": {"passphrase"}}) is True


def test_secrets_only_diff_false_when_other_attr_changes() -> None:
    plan = {"resource_changes": [{
        "type": "unifi_wlan", "address": "unifi_wlan.iot",
        "change": {"actions": ["update"],
                   "before": {"name": "a"}, "after": {"name": "b"}}}]}
    assert is_secrets_only_diff(plan, {"unifi_wlan": {"passphrase"}}) is False


def test_secrets_only_diff_false_on_delete() -> None:
    """Delete is a structural change, never 'secrets only'."""
    plan = {"resource_changes": [{
        "type": "unifi_wlan", "address": "unifi_wlan.iot",
        "change": {"actions": ["delete"],
                   "before": {"passphrase": "secret", "name": "iot"},
                   "after": {}}}]}
    assert is_secrets_only_diff(plan, {"unifi_wlan": {"passphrase"}}) is False


def test_secrets_only_diff_false_on_replace() -> None:
    """Replace (create+delete pair) is structural, never 'secrets only'."""
    plan = {"resource_changes": [{
        "type": "unifi_wlan", "address": "unifi_wlan.iot",
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
                           removed=[], secret_warnings=["wlan_guest_psk"])
    assert "wlan_guest_psk" in out
    assert "TF_VAR_wlan_guest_psk" in out


def test_format_reconcile_reports_orphaned_state():
    from ubitofu.reporter import format_reconcile

    out = format_reconcile(merged=[], complex_flags=[], appended=[], removed=[],
                           orphaned=["unifi_port_forward.traefik_preview"])
    assert "traefik_preview" in out
    assert "DESTROY" in out.upper()
