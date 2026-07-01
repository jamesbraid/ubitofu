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


def test_wireguard_two_level(fixtures_dir):
    # The trickiest quirk: WG peers enumerate in TWO levels — first list the
    # WireGuard-server networks, then GET each server's users. Import id is
    # "<network_id>:<peer_id>" (verified against the real oracle imports.tf).
    (fixtures_dir / "wg_net.json").write_text(
        '{"data":[{"_id":"wgnet1","name":"examplenet",'
        '"purpose":"remote-user-vpn","vpn_type":"wireguard-server"}]}')
    (fixtures_dir / "wg_users.json").write_text(
        '[{"_id":"peerA","name":"sputnik","network_id":"wgnet1"},'
        '{"_id":"peerB","name":"ghostrider","network_id":"wgnet1"}]')
    ctl = FakeController(fixtures_dir, {
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
    assert [t.import_id for t in res.targets] == ["aa:bb:cc:00:00:01"]  # only fixed_ip one


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


def test_account_alias_not_double_imported(fixtures_dir):
    # rest/account backs both unifi_radius_user and the unifi_account alias.
    (fixtures_dir / "account.json").write_text('{"data":[{"_id":"acc1","name":"radius-u"}]}')
    ctl = FakeController(fixtures_dir, {"rest/account": "account.json"})
    res = enumerate_controller(ctl, manifest=[
        s for s in MANIFEST if s.endpoint == "rest/account"])
    types = [t.resource_type for t in res.targets]
    assert types == ["unifi_radius_user"]  # unifi_account alias skipped


def test_unmapped_populated_collection_is_flagged(fixtures_dir):
    (fixtures_dir / "nat.json").write_text('[{"_id":"n1"},{"_id":"n2"}]')

    class C(FakeController):
        def collection(self, endpoint):
            if endpoint == "v2/api/site/{site}/nat":
                return [{"_id": "n1"}, {"_id": "n2"}]
            return []

    res = enumerate_controller(C(fixtures_dir, {}), manifest=MANIFEST)
    assert any("2 objects" in g and "nat" in g for g in res.gaps)
