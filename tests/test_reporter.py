from unifi_tofu_import.reporter import (
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
