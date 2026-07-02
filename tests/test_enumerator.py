import json

from unifi_tofu_import.controller import Controller
from unifi_tofu_import.enumerator import ImportTarget, enumerate_controller
from unifi_tofu_import.manifest import MANIFEST


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
