"""Site B without an access port: a new VSI/VPLS with only PE A's loopback as LDP peer, or one more peer on a VPLS
instance already on PE B (Huawei VSI, ZTE VPLS, IOS-XR bridge-domain / VFI, FiberHome VFI)."""

import pytest

from app.services import running_config
from app.services.configgen import AddressError, Addressing, generate, parse_b_join
from app.services.l2vpn_peers import vpls_instances, vpls_key
from tests.conftest import config_text
from tests.test_configgen import _vendor, client, make_service  # noqa: F401  (fixtures)
from tests.test_validation import net  # noqa: F401

XR = config_text("l2vpn/iosxr_vpls.cfg")
VRP = config_text("l2vpn/vrp_vsi.cfg")
ZTE = config_text("l2vpn/zte_vpls.cfg")
FH = config_text("l2vpn/fiberhome_vfi.cfg")
VFI = vpls_key("Corporate", "vpls-1919", "vpls-1919")
PE_A = "10.0.0.1"  # every PE of the test network has this loopback


def _site_b(db, make_service, dialect: str, text: str, vlans: list[int], **addressing):  # noqa: F811
    service = make_service(dialect, "l2vpn", vlan_id=vlans[0], b_interface=None)
    result = generate(db, service, Addressing(vlans=vlans, b_no_port=True, **addressing),
                      config_source=lambda device_id: text)
    return result


# --------------------------------------------------------------------------- reading instances


def test_should_list_vfi_and_bridge_domain_instances_with_their_interfaces_and_pw_ids():
    rows = {row["key"]: row for row in vpls_instances(XR, "iosxr")}
    assert set(rows) == {VFI, vpls_key("Corporate", "VPLS-2600", None)}
    assert rows[VFI]["interfaces"] == ["Bundle-Ether10.1919", "Bundle-Ether35.1919"]
    assert (rows[VFI]["pw_ids"], rows[VFI]["vlans"]) == ([1919], [1919])


def test_should_list_ldp_vsi_with_vsi_id_peers_and_bound_interfaces_when_device_is_vrp():
    rows = {row["name"]: row for row in vpls_instances(VRP, "vrp")}
    assert rows["CUST_1490"]["vsi_id"] == 12490
    assert [n["ip"] for n in rows["CUST_1491"]["neighbours"]] == ["10.20.0.52", PE_A]
    assert (rows["CUST_1490"]["interfaces"], rows["CUST_1490"]["vlans"]) == (["Virtual-Ethernet0/1/0.2290"], [2290])


def test_should_leave_out_bgp_signalled_vsi_when_listing_vrp_instances():
    assert "KOMPELLA_10" not in {row["name"] for row in vpls_instances(VRP, "vrp")}


def test_should_take_the_peer_pw_id_from_negotiation_vc_id_when_vrp_peer_sets_one():
    other = next(row for row in vpls_instances(VRP, "vrp") if row["name"] == "OTHER_77")
    assert other["pw_ids"] == [7700]


def test_should_list_zte_vpls_with_pw_names_vcids_and_access_points():
    row = next(row for row in vpls_instances(ZTE, "zte") if row["name"] == "VPLS_1489")
    assert row["interfaces"] == ["smartgroup81.1489", "smartgroup82.1489"]
    assert [(n["ip"], n["pw"], n["pw_id"]) for n in row["neighbours"]] == [("10.20.0.2", "pw1", 25489),
                                                                           ("10.20.0.3", "pw3", 25489)]


def test_should_list_fiberhome_vfi_with_vsi_id_as_pw_id_of_its_peers():
    row = vpls_instances(FH, "fiberhome")[0]
    assert (row["name"], row["vsi_id"], row["pw_ids"], row["vlans"]) == ("VPLS_1490", 11490, [11490], [1490])


# --------------------------------------------------------------------------- new instance without an access port


def test_should_build_one_vsi_per_vlan_without_attachment_circuit_when_vrp_site_b_has_no_port(db, make_service):  # noqa: F811
    b = _site_b(db, make_service, "vrp", VRP, [3, 4, 55])["endpoints"]["B"]
    assert b["config"][:10] == ["system-view", "vsi PT-CONTOH_3", " pwsignal ldp", "  vsi-id 3", "  control-word enable",
                                f"  peer {PE_A}", "  quit", " mtu 2000", " encapsulation ethernet", " quit"]
    assert not any(line.startswith(("interface", " l2 binding")) for line in b["config"])
    assert b["rollback"] == ["system-view", "undo vsi PT-CONTOH_3", "undo vsi PT-CONTOH_4", "undo vsi PT-CONTOH_55"]


def test_should_write_zte_vpls_with_pw_but_no_access_point_when_site_b_has_no_port(db, make_service):  # noqa: F811
    b = _site_b(db, make_service, "zte", ZTE, [300])["endpoints"]["B"]
    assert b["config"] == ["configure terminal", "pw pw4", "vpls PT-CONTOH_300", "  mtu 2000", "  pseudo-wire pw4",
                           f"    neighbour {PE_A} vcid 300 cw-preferred", "    $", "  $", "$"]


def test_should_write_iosxr_bridge_domain_with_neighbour_only_when_site_b_has_no_port(db, make_service):  # noqa: F811
    b = _site_b(db, make_service, "iosxr", XR, [300])["endpoints"]["B"]
    assert b["config"][:5] == ["configure", "l2vpn", " bridge group Corporate", "  bridge-domain PT-CONTOH_300",
                               "   mtu 2000"]
    assert b["rollback"] == ["configure", "no l2vpn bridge group Corporate bridge-domain PT-CONTOH_300"]


def test_should_write_fiberhome_vfi_with_peer_only_when_site_b_has_no_port(db, make_service):  # noqa: F811
    b = _site_b(db, make_service, "fiberhome", FH, [300])["endpoints"]["B"]
    assert b["config"] == ["configure terminal", "vfi PT-CONTOH_300", " vsi-id 300", " mtu 2000",
                           f" peer {PE_A} encapsulation raw", "  exit", " exit"]


# --------------------------------------------------------------------------- joining an existing instance


def test_should_add_only_a_neighbour_to_the_vfi_when_site_b_joins_an_existing_vpls(db, make_service):  # noqa: F811
    b = _site_b(db, make_service, "iosxr", XR, [1919], b_join={1919: VFI})["endpoints"]["B"]
    assert b["config"] == ["configure", "l2vpn", " bridge group Corporate", "  bridge-domain vpls-1919",
                           "   vfi vpls-1919", f"    neighbor {PE_A} pw-id 1919", "     pw-class FAT-CW", "root"]
    assert f"    no neighbor {PE_A} pw-id 1919" in b["rollback"]


def test_should_write_the_neighbour_on_the_bridge_domain_when_the_instance_has_no_vfi(db, make_service):  # noqa: F811
    key = vpls_key("Corporate", "VPLS-2600", None)
    b = _site_b(db, make_service, "iosxr", XR, [2600], b_join={2600: key})["endpoints"]["B"]
    assert f"   neighbor {PE_A} pw-id 2600" in b["config"] and "   vfi" not in " ".join(b["config"])


def test_should_add_a_peer_to_the_vsi_and_use_its_vsi_id_on_both_sites_when_vrp_site_b_joins(db, make_service):  # noqa: F811
    result = _site_b(db, make_service, "vrp", VRP, [1490], b_join={1490: "vsi|CUST_1490"})
    assert result["endpoints"]["B"]["config"] == ["system-view", "vsi CUST_1490", " pwsignal ldp", f"  peer {PE_A}",
                                                  "  quit", " quit"]
    assert "  vsi-id 12490" in result["endpoints"]["A"]["config"]


def test_should_write_negotiation_vc_id_when_the_typed_vc_id_differs_from_the_vsi_id(db, make_service):  # noqa: F811
    b = _site_b(db, make_service, "vrp", VRP, [1490], b_join={1490: "vsi|CUST_1490"}, vc_id=5000)["endpoints"]["B"]
    assert f"  peer {PE_A} negotiation-vc-id 5000" in b["config"]


def test_should_skip_the_vlan_when_pe_a_is_already_a_peer_of_the_chosen_vsi(db, make_service):  # noqa: F811
    result = _site_b(db, make_service, "vrp", VRP, [1490, 1491],
                     b_join={1490: "vsi|CUST_1490", 1491: "vsi|CUST_1491"})
    b = result["endpoints"]["B"]
    assert "vsi CUST_1491" not in b["config"]
    assert [row["action"] for row in b["vpls"]] == ["join", "skip"]


def test_should_add_a_new_pw_with_the_instance_split_horizon_when_zte_site_b_joins(db, make_service):  # noqa: F811
    b = _site_b(db, make_service, "zte", ZTE, [1489], b_join={1489: "vpls|VPLS_1489"})["endpoints"]["B"]
    assert b["config"] == ["configure terminal", "pw pw4", "vpls VPLS_1489", "  pseudo-wire pw4 split-horizon scope B",
                           f"    neighbour {PE_A} vcid 1489 cw-preferred", "    $", "  $", "$"]
    assert b["rollback"] == ["configure terminal", "vpls VPLS_1489", "  no pseudo-wire pw4", "$", "no pw pw4"]


def test_should_use_the_vfi_vsi_id_as_vc_id_when_fiberhome_site_b_joins(db, make_service):  # noqa: F811
    result = _site_b(db, make_service, "fiberhome", FH, [1490], b_join={1490: "vfi|VPLS_1490"})
    assert result["endpoints"]["B"]["config"] == ["configure terminal", "vfi VPLS_1490",
                                                  f" peer {PE_A} encapsulation raw", "  exit", " exit"]
    assert result["endpoints"]["B"]["vc_ids"][0] == {"vlan": 1490, "vc_id": 11490, "pw": None, "conflicts": []}


def test_should_mix_a_joined_vsi_and_a_new_vsi_when_only_some_vlans_join(db, make_service):  # noqa: F811
    b = _site_b(db, make_service, "vrp", VRP, [1490, 1492], b_join={1490: "vsi|CUST_1490"})["endpoints"]["B"]
    assert [row["action"] for row in b["vpls"]] == ["join", "new"]
    assert b["config"].count("system-view") == 1 and "vsi PT-CONTOH_1492" in b["config"]


def test_should_ask_to_pick_again_when_the_chosen_vsi_is_not_in_the_config(db, make_service):  # noqa: F811
    b = _site_b(db, make_service, "vrp", VRP, [1490], b_join={1490: "vsi|GONE_1"})["endpoints"]["B"]
    assert not b["config"] and any("pilih ulang" in w for w in b["warnings"])


def test_should_refuse_the_same_vsi_for_two_vlans():
    with pytest.raises(AddressError):
        parse_b_join(["3=vsi|X", "4=vsi|X"], [3, 4])


def test_should_keep_dialect_and_ask_for_a_port_or_no_port_vsi_when_site_b_has_neither(db, make_service):  # noqa: F811
    service = make_service("iosxr", "l2vpn", b_interface=None)
    b = generate(db, service, Addressing(vlans=[300]), config_source=lambda device_id: XR)["endpoints"]["B"]
    assert b["dialect"] == "iosxr" and not b["config"]
    assert any("tanpa port akses" in w for w in b["warnings"])


# --------------------------------------------------------------------------- API


def test_should_build_huawei_site_b_without_port_when_site_a_is_zte_with_three_vlans(client, db, make_service, net,  # noqa: F811
                                                                                    monkeypatch):
    make_service("zte", "l2vpn")
    net["dev"]["B-PE1"].vendor_id = _vendor(db, "vrp").id
    db.commit()
    monkeypatch.setattr(running_config, "fetch", lambda db, device_id: VRP if device_id == net["dev"]["B-PE1"].id
                        else ZTE)
    response = client.get("/api/configs/l2vpn", params={
        "a_device_id": net["dev"]["A-PE1"].id, "a_port": "smartgroup81", "b_device_id": net["dev"]["B-PE1"].id,
        "vlans": "3-4,55", "b_no_port": "true"})
    assert response.status_code == 200, response.text
    endpoints = response.json()["endpoints"]
    assert [line for line in endpoints["B"]["config"] if line.startswith("vsi ")] == ["vsi L2VPN_3", "vsi L2VPN_4",
                                                                                     "vsi L2VPN_55"]
    assert endpoints["A"]["dialect"] == "zte" and "  access-point smartgroup81.55" in endpoints["A"]["config"]


def test_should_join_per_vlan_vsi_from_the_b_join_query(client, make_service, net, monkeypatch):  # noqa: F811
    make_service("iosxr", "l2vpn")
    monkeypatch.setattr(running_config, "fetch", lambda db, device_id: XR)
    response = client.get("/api/configs/l2vpn", params={
        "a_device_id": net["dev"]["A-PE1"].id, "a_port": "Bundle-Ether1", "b_device_id": net["dev"]["B-PE1"].id,
        "vlans": "1919", "b_join": f"1919={VFI}"})
    assert response.status_code == 200, response.text
    assert f"    neighbor {PE_A} pw-id 1919" in response.json()["endpoints"]["B"]["config"]


def test_should_reject_a_b_join_for_a_vlan_outside_the_list(client, make_service, net):  # noqa: F811
    make_service("iosxr", "l2vpn")
    response = client.get("/api/configs/l2vpn", params={
        "a_device_id": net["dev"]["A-PE1"].id, "a_port": "Bundle-Ether1", "b_device_id": net["dev"]["B-PE1"].id,
        "vlans": "1919", "b_join": f"7={VFI}"})
    assert response.status_code == 422


def test_should_list_vsi_instances_of_a_huawei_pe(client, make_service, net, monkeypatch):  # noqa: F811
    make_service("vrp", "l2vpn")
    monkeypatch.setattr(running_config, "fetch", lambda db, device_id: VRP)
    body = client.get("/api/configs/l2vpn/vpls", params={"device_id": net["dev"]["B-PE1"].id}).json()
    assert body["supported"] and [row["name"] for row in body["instances"]] == ["CUST_1490", "CUST_1491", "OTHER_77"]


def test_should_not_list_instances_for_a_device_without_generator_dialect(client, db, net):  # noqa: F811
    assert not client.get("/api/configs/l2vpn/vpls", params={"device_id": net["dev"]["B-PE1"].id}).json()["supported"]


