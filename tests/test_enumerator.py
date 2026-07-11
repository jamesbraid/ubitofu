# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 James Braid
import json

from ubitofu.controller import Controller
from ubitofu.enumerator import ImportTarget, _name_hint, enumerate_controller
from ubitofu.manifest import MANIFEST, ResourceSpec, spec_for_type


class FakeController(Controller):
    def __init__(self, fixtures_dir, populated):
        self.site = "default"
        self._dir = fixtures_dir
        self._populated = populated  # endpoint -> fixture filename

    def collection(self, endpoint):
        fname = self._populated.get(endpoint)
        if fname is None:
            return []
        data = json.loads((self._dir / fname).read_text())
        return data["data"] if isinstance(data, dict) else data


def test_networkconf_discriminated_and_guest_skipped(fixtures_dir):
    ctl = FakeController(fixtures_dir, {"rest/networkconf": "networkconf.json"})
    res = enumerate_controller(ctl, manifest=[
        s for s in MANIFEST if s.endpoint == "rest/networkconf"])
    kinds = {(t.resource_type, t.name_hint) for t in res.targets}
    assert ("unifi_network", "lan") in kinds
    assert ("unifi_wan", "wan1") in kinds
    assert ("unifi_vpn_server", "vpn") in kinds
    assert not any(t.name_hint == "guestnet" for t in res.targets)  # guest skipped


def test_wireguard_two_level(tmp_path):
    # The trickiest quirk: WG peers enumerate in TWO levels — first list the
    # WireGuard-server networks, then GET each server's users. Import id is
    # "<network_id>:<peer_id>" (verified against the real oracle imports.tf).
    (tmp_path / "wg_net.json").write_text(
        '{"data":[{"_id":"wgnet1","name":"examplenet",'
        '"purpose":"remote-user-vpn","vpn_type":"wireguard-server"}]}')
    (tmp_path / "wg_users.json").write_text(
        '[{"_id":"peerA","name":"alice-laptop","network_id":"wgnet1"},'
        '{"_id":"peerB","name":"bob-phone","network_id":"wgnet1"}]')
    ctl = FakeController(tmp_path, {
        "rest/networkconf": "wg_net.json",
        "v2/api/site/{site}/wireguard/wgnet1/users": "wg_users.json",
    })
    res = enumerate_controller(ctl, manifest=[
        s for s in MANIFEST if s.resource_type == "unifi_wireguard_peer"])
    assert {t.import_id for t in res.targets} == {"wgnet1:peerA", "wgnet1:peerB"}
    assert all(t.resource_type == "unifi_wireguard_peer" for t in res.targets)


def test_client_filter_requires_fixed_ip(fixtures_dir):
    ctl = FakeController(fixtures_dir, {"rest/user": "user.json"})
    res = enumerate_controller(ctl, manifest=[
        s for s in MANIFEST if s.resource_type == "unifi_client"])
    assert [t.import_id for t in res.targets] == ["00:11:22:00:00:01"]  # only fixed_ip one


def test_firewall_policy_predefined_filtered(fixtures_dir):
    ctl = FakeController(
        fixtures_dir, {"v2/api/site/{site}/firewall-policies": "firewall-policies.json"}
    )
    res = enumerate_controller(ctl, manifest=[
        s for s in MANIFEST if s.resource_type == "unifi_firewall_policy"])
    assert [t.import_id for t in res.targets] == ["fp1"]


def test_singleton_setting_imports_by_site(fixtures_dir):
    ctl = FakeController(fixtures_dir, {"get/setting": None})  # non-enumerable
    res = enumerate_controller(ctl, manifest=[
        s for s in MANIFEST if s.resource_type == "unifi_setting"])
    assert res.targets == [ImportTarget("unifi_setting", "setting", "default")]


def test_account_alias_not_double_imported(tmp_path):
    # rest/account backs both unifi_radius_user and the unifi_account alias.
    (tmp_path / "account.json").write_text('{"data":[{"_id":"acc1","name":"radius-u"}]}')
    ctl = FakeController(tmp_path, {"rest/account": "account.json"})
    res = enumerate_controller(ctl, manifest=[
        s for s in MANIFEST if s.endpoint == "rest/account"])
    types = [t.resource_type for t in res.targets]
    assert types == ["unifi_radius_user"]  # unifi_account alias skipped


def test_app_based_firewall_policy_skipped_and_reported(tmp_path):
    # A unifi_firewall_policy whose source/destination matching_target is "APP"
    # (DPI / app_ids) cannot be represented by the provider — skip + report.
    (tmp_path / "fw_app.json").write_text(
        '{"data":['
        '{"_id":"fp1","name":"normal","predefined":false,'
        '"source":{"matching_target":"NETWORK"},'
        '"destination":{"matching_target":"NETWORK"}},'
        '{"_id":"fp2","name":"block dns","predefined":false,'
        '"source":{"matching_target":"CLIENT"},'
        '"destination":{"matching_target":"APP"}}]}')
    ctl = FakeController(tmp_path, {
        "v2/api/site/{site}/firewall-policies": "fw_app.json"})
    res = enumerate_controller(ctl, manifest=[
        s for s in MANIFEST if s.resource_type == "unifi_firewall_policy"])
    assert [t.import_id for t in res.targets] == ["fp1"]  # APP one skipped
    assert any("app-based" in g and "APP" in g for g in res.gaps)


def test_guest_network_reported_as_gap(fixtures_dir):
    ctl = FakeController(fixtures_dir, {"rest/networkconf": "networkconf.json"})
    res = enumerate_controller(ctl, manifest=[
        s for s in MANIFEST if s.endpoint == "rest/networkconf"])
    assert any("guest network" in g for g in res.gaps)


def test_bgp_singleton_skipped_when_unconfigured(fixtures_dir):
    # bgp/config returns [] when BGP is off -> importing by site would fail.
    ctl = FakeController(fixtures_dir, {})  # bgp endpoint -> [] (empty)
    res = enumerate_controller(ctl, manifest=[
        s for s in MANIFEST if s.resource_type == "unifi_bgp"])
    assert res.targets == []
    assert any("unifi_bgp" in g for g in res.gaps)


def test_bgp_singleton_kept_when_configured(tmp_path):
    (tmp_path / "bgp.json").write_text('[{"_id":"b1","as_number":65000}]')
    ctl = FakeController(tmp_path, {
        "v2/api/site/{site}/bgp/config": "bgp.json"})
    res = enumerate_controller(ctl, manifest=[
        s for s in MANIFEST if s.resource_type == "unifi_bgp"])
    assert [t.import_id for t in res.targets] == ["default"]


def test_default_radius_profile_skipped_and_reported(tmp_path):
    (tmp_path / "radius.json").write_text(
        '{"data":[{"_id":"r1","name":"Default","attr_no_delete":true,'
        '"attr_hidden_id":"Default"}]}')
    ctl = FakeController(tmp_path, {"rest/radiusprofile": "radius.json"})
    res = enumerate_controller(ctl, manifest=[
        s for s in MANIFEST if s.resource_type == "unifi_radius_profile"])
    assert res.targets == []
    assert any("radius" in g.lower() for g in res.gaps)


def test_default_usergroup_qos_rate_skipped_and_reported(tmp_path):
    (tmp_path / "ug.json").write_text(
        '{"data":[{"_id":"g1","name":"Default","attr_no_delete":true,'
        '"attr_hidden_id":"Default","qos_rate_max_up":-1,"qos_rate_max_down":-1}]}')
    ctl = FakeController(tmp_path, {"rest/usergroup": "ug.json"})
    res = enumerate_controller(ctl, manifest=[
        s for s in MANIFEST if s.resource_type == "unifi_client_qos_rate"])
    assert res.targets == []
    assert any("qos" in g.lower() or "usergroup" in g.lower() for g in res.gaps)


def test_dns_record_name_hint_uses_key(tmp_path):
    # static-dns keys the hostname under `key` (name is null) -> readable slug.
    (tmp_path / "sdns.json").write_text(
        '[{"_id":"66c0","key":"home.example.org","record_type":"A",'
        '"value":"192.0.2.250","name":null}]')
    ctl = FakeController(tmp_path, {
        "v2/api/site/{site}/static-dns": "sdns.json"})
    res = enumerate_controller(ctl, manifest=[
        s for s in MANIFEST if s.resource_type == "unifi_dns_record"])
    assert [t.name_hint for t in res.targets] == ["home.example.org"]


def test_dynamic_dns_name_hint_uses_host_name(tmp_path):
    (tmp_path / "ddns.json").write_text(
        '[{"_id":"66c9","host_name":"example-home.example.net",'
        '"service":"dyndns","name":null,"key":null}]')
    ctl = FakeController(tmp_path, {"rest/dynamicdns": "ddns.json"})
    res = enumerate_controller(ctl, manifest=[
        s for s in MANIFEST if s.resource_type == "unifi_dynamic_dns"])
    assert [t.name_hint for t in res.targets] == ["example-home.example.net"]


def test_unmapped_populated_collection_is_flagged(tmp_path):
    class C(FakeController):
        def collection(self, endpoint):
            if endpoint == "v2/api/site/{site}/nat":
                return [{"_id": "n1"}, {"_id": "n2"}]
            return []

    res = enumerate_controller(C(tmp_path, {}), manifest=MANIFEST)
    assert any("2 objects" in g and "nat" in g for g in res.gaps)


# --------------------------------------------------------------------------
# Mutation-hardening tests for enumerator.py.
# --------------------------------------------------------------------------

_FW_SPEC = [s for s in MANIFEST if s.resource_type == "unifi_firewall_policy"]
_FG_SPEC = [s for s in MANIFEST if s.resource_type == "unifi_firewall_group"]


def test_app_policy_source_side_app_is_skipped(tmp_path):
    # _is_app_policy must inspect BOTH "source" and "destination". A policy whose
    # SOURCE (not destination) matches on APP must still be skipped — this exercises
    # the "source" iteration that a mangled loop tuple would silently drop.
    (tmp_path / "fw.json").write_text(
        '{"data":[{"_id":"fp1","name":"src app","predefined":false,'
        '"source":{"matching_target":"APP"},'
        '"destination":{"matching_target":"NETWORK"}}]}')
    ctl = FakeController(tmp_path, {"v2/api/site/{site}/firewall-policies": "fw.json"})
    res = enumerate_controller(ctl, manifest=_FW_SPEC)
    assert res.targets == []  # source APP -> skipped, not emitted
    assert any("app-based" in g for g in res.gaps)


def test_two_app_policies_exact_count_and_trailing_normal_emitted(tmp_path):
    # Two APP policies (skipped) FOLLOWED by a normal one (emitted). Pins the exact
    # aggregated skip count (=2) and proves the per-object skip uses `continue`
    # (not `break`) so the trailing normal policy is still reached.
    (tmp_path / "fw.json").write_text(
        '{"data":['
        '{"_id":"a1","name":"app one","predefined":false,'
        '"source":{"matching_target":"CLIENT"},'
        '"destination":{"matching_target":"APP"}},'
        '{"_id":"a2","name":"app two","predefined":false,'
        '"source":{"matching_target":"CLIENT"},'
        '"destination":{"matching_target":"APP"}},'
        '{"_id":"n1","name":"normal","predefined":false,'
        '"source":{"matching_target":"NETWORK"},'
        '"destination":{"matching_target":"NETWORK"}}]}')
    ctl = FakeController(tmp_path, {"v2/api/site/{site}/firewall-policies": "fw.json"})
    res = enumerate_controller(ctl, manifest=_FW_SPEC)
    assert [t.import_id for t in res.targets] == ["n1"]  # both APP skipped, normal kept
    assert ("2 app-based firewall policy(ies) — unsupported matching_target=APP"
            in res.gaps)


def test_skip_reason_uses_and_not_or_for_qos_default(tmp_path):
    # attr_no_delete on a non-qos, non-radius resource must NOT trigger the
    # usergroup-default skip: the guard is `rt == qos AND attr_no_delete`, so a
    # firewall_group carrying attr_no_delete is still emitted.
    (tmp_path / "fg.json").write_text(
        '{"data":[{"_id":"fg1","name":"grp","attr_no_delete":true}]}')
    ctl = FakeController(tmp_path, {"rest/firewallgroup": "fg.json"})
    res = enumerate_controller(ctl, manifest=_FG_SPEC)
    assert [t.import_id for t in res.targets] == ["fg1"]  # not skipped
    assert not any("usergroup" in g.lower() for g in res.gaps)


def test_name_hint_direct_fallback_chain():
    spec = ResourceSpec("unifi_network", "rest/networkconf", "_id")  # non-site
    # Nothing usable present -> fall all the way through to `site` (the final `or`).
    assert _name_hint({}, spec, "mysite") == "mysite"
    # `hostname` is consulted when `name` is absent.
    assert _name_hint({"hostname": "myhost", "_id": "x"}, spec, "s") == "myhost"
    # `_id` is consulted when name/hostname/host_name/key are all absent.
    assert _name_hint({"_id": "abc"}, spec, "s") == "abc"


def test_name_hint_falls_back_to_site_via_enumerate(tmp_path):
    # A tofu-state-shaped row carries `id` (not `_id`) and no name: _name_hint has
    # nothing to latch onto and must fall back to the site string. Pins the site
    # argument threaded into the _name_hint call site.
    (tmp_path / "fg.json").write_text('{"data":[{"id":"fg9"}]}')
    ctl = FakeController(tmp_path, {"rest/firewallgroup": "fg.json"})
    res = enumerate_controller(ctl, manifest=_FG_SPEC)
    assert [t.name_hint for t in res.targets] == ["default"]
    assert [t.import_id for t in res.targets] == ["fg9"]


def test_extract_id_threads_site_for_composite_rule(tmp_path):
    # A site:_id rule builds "<site>:<object_id>", so extract_id MUST receive the
    # real site (not a dropped/None arg). Uses a synthetic spec since no manifest
    # entry currently uses site:_id.
    (tmp_path / "ex.json").write_text('{"data":[{"_id":"obj1"}]}')
    spec = ResourceSpec("unifi_hypothetical", "rest/example", "site:_id")
    ctl = FakeController(tmp_path, {"rest/example": "ex.json"})
    res = enumerate_controller(ctl, manifest=[spec])
    assert [t.import_id for t in res.targets] == ["default:obj1"]


def test_guest_network_gap_exact_count(fixtures_dir):
    # The networkconf fixture has exactly one guest network; pin the rendered count
    # and the guest-only filter (a `!= "guest"` mutation would count the others).
    ctl = FakeController(fixtures_dir, {"rest/networkconf": "networkconf.json"})
    res = enumerate_controller(ctl, manifest=[
        s for s in MANIFEST if s.endpoint == "rest/networkconf"])
    assert ("1 guest network(s) — no provider resource; not imported"
            in res.gaps)


def test_wireguard_injects_network_id_and_derives_name_hint(tmp_path):
    # Peers deliberately LACK a network_id field, so the enumerator's injection of
    # network_id is load-bearing for the composite id. Also pins name_hint:
    # peerA (has name) -> name; peerB (no name) -> its _id (not the import_id).
    (tmp_path / "wg_net.json").write_text(
        '{"data":[{"_id":"wgnet1","purpose":"remote-user-vpn",'
        '"vpn_type":"wireguard-server"}]}')
    (tmp_path / "wg_users.json").write_text(
        '[{"_id":"peerA","name":"alice-laptop"},{"_id":"peerB"}]')
    ctl = FakeController(tmp_path, {
        "rest/networkconf": "wg_net.json",
        "v2/api/site/{site}/wireguard/wgnet1/users": "wg_users.json",
    })
    res = enumerate_controller(ctl, manifest=[
        s for s in MANIFEST if s.resource_type == "unifi_wireguard_peer"])
    by_id = {t.import_id: t for t in res.targets}
    assert set(by_id) == {"wgnet1:peerA", "wgnet1:peerB"}  # injection load-bearing
    assert by_id["wgnet1:peerA"].name_hint == "alice-laptop"  # name used
    assert by_id["wgnet1:peerB"].name_hint == "peerB"  # _id fallback, not import_id


def _acct(tmp_path):
    (tmp_path / "account.json").write_text('{"data":[{"_id":"acc1","name":"u"}]}')


def test_alias_skip_continues_to_later_specs(tmp_path):
    # The unifi_account alias is skipped, but a spec AFTER it must still run:
    # the skip is `continue`, not `break`.
    _acct(tmp_path)
    ctl = FakeController(tmp_path, {"rest/account": "account.json"})
    res = enumerate_controller(ctl, manifest=[
        spec_for_type("unifi_account"), spec_for_type("unifi_radius_user")])
    assert any(t.resource_type == "unifi_radius_user" for t in res.targets)


def test_singleton_skip_if_empty_continues_to_later_specs(tmp_path):
    # An empty skip_if_empty singleton (bgp) records a gap and CONTINUES; a later
    # spec must still be enumerated.
    _acct(tmp_path)
    ctl = FakeController(tmp_path, {"rest/account": "account.json"})  # bgp endpoint -> []
    res = enumerate_controller(ctl, manifest=[
        spec_for_type("unifi_bgp"), spec_for_type("unifi_radius_user")])
    assert any("unifi_bgp" in g for g in res.gaps)  # the skip branch fired
    assert any(t.resource_type == "unifi_radius_user" for t in res.targets)


def test_singleton_emit_continues_to_later_specs(tmp_path):
    # After emitting a site singleton (setting), a later spec must still run:
    # the post-append is `continue`, not `break`.
    _acct(tmp_path)
    ctl = FakeController(tmp_path, {"rest/account": "account.json"})  # get/setting -> []
    res = enumerate_controller(ctl, manifest=[
        spec_for_type("unifi_setting"), spec_for_type("unifi_radius_user")])
    types = {t.resource_type for t in res.targets}
    assert "unifi_setting" in types
    assert "unifi_radius_user" in types


def test_wireguard_branch_continues_to_later_specs(tmp_path):
    # After the wg two-level branch extends targets, a later spec must still run.
    _acct(tmp_path)
    (tmp_path / "wg_net.json").write_text(
        '{"data":[{"_id":"wgnet1","purpose":"remote-user-vpn",'
        '"vpn_type":"wireguard-server"}]}')
    (tmp_path / "wg_users.json").write_text('[{"_id":"peerA","name":"a"}]')
    ctl = FakeController(tmp_path, {
        "rest/networkconf": "wg_net.json",
        "v2/api/site/{site}/wireguard/wgnet1/users": "wg_users.json",
        "rest/account": "account.json",
    })
    res = enumerate_controller(ctl, manifest=[
        spec_for_type("unifi_wireguard_peer"), spec_for_type("unifi_radius_user")])
    types = {t.resource_type for t in res.targets}
    assert "unifi_wireguard_peer" in types
    assert "unifi_radius_user" in types
