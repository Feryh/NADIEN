"""L2VPN between two hostnames: existing neighbours from the running config, the chosen neighbour, the PW id."""

import pytest

from app.services import running_config
from app.services.configgen import Addressing, generate
from app.services.l2vpn_peers import neighbours, pw_usage
from tests.test_configgen import client, make_service  # noqa: F401  (fixtures)
from tests.test_validation import net  # noqa: F401

VRP = """vsi OXY_1480
 pwsignal ldp
  vsi-id 1480
  peer 10.1.1.1
  peer 10.1.1.2
 mtu 2000
#
interface GigabitEthernet0/3/1.20
 mpls l2vc 10.1.1.1 20
#
"""
ZTE = """vpls OXY_1480
  pseudo-wire pw4
    neighbour 10.2.2.2 vcid 1481 cw-preferred
"""
XR = """l2vpn
 xconnect group xg_OXY
  p2p OXY_1480
   neighbor ipv4 10.3.3.3 pw-id 1480
"""
FH = """vfi OXY_1480
 vsi-id 1480
 peer 10.4.4.4 encapsulation raw
  exit
 exit
"""


@pytest.mark.parametrize("dialect, text, expected", [
    ("vrp", VRP, {("10.1.1.1", 1480), ("10.1.1.2", 1480), ("10.1.1.1", 20)}),
    ("zte", ZTE, {("10.2.2.2", 1481)}),
    ("iosxr", XR, {("10.3.3.3", 1480)}),
    ("fiberhome", FH, {("10.4.4.4", 1480)}),
])
def test_should_read_neighbour_and_pw_id_pairs_when_parsing_each_dialect(dialect, text, expected):
    assert set(pw_usage(text, dialect)) == expected


def test_should_list_busiest_neighbour_first_with_its_pw_ids():
    rows = neighbours(VRP, "vrp")
    assert rows[0] == {"ip": "10.1.1.1", "pw_ids": [20, 1480], "count": 2}


def test_should_write_chosen_neighbour_when_peer_is_given(db, make_service):
    a = generate(db, make_service("zte", "l2vpn"), Addressing(vlans=[1480], a_peer="10.9.9.9"),
                 config_source=lambda device_id: "")["endpoints"]["A"]
    assert "    neighbour 10.9.9.9 vcid 1480 cw-preferred" in a["config"]


def test_should_flag_pw_id_already_used_to_the_neighbour_when_device_is_huawei(db, make_service):
    a = generate(db, make_service("vrp", "l2vpn"), Addressing(vlans=[1480], a_peer="10.1.1.1"),
                 config_source=lambda device_id: VRP)["endpoints"]["A"]
    assert a["vc_ids"][0]["conflicts"] and any("vcid 1480" in w for w in a["warnings"])


def test_should_build_l2vpn_from_two_hostnames_when_no_service_exists(client, make_service, net, monkeypatch):  # noqa: F811
    make_service("vrp", "l2vpn")  # sets the vendors; the request itself names no service
    monkeypatch.setattr(running_config, "fetch", lambda db, device_id: VRP)
    response = client.get("/api/configs/l2vpn", params={
        "a_device_id": net["dev"]["A-PE1"].id, "a_port": "GigabitEthernet0/3/1",
        "b_device_id": net["dev"]["B-PE1"].id, "b_port": "GigabitEthernet0/3/2",
        "vlans": "1490", "b_peer": "10.5.5.5", "name": "OXY"})
    assert response.status_code == 200, response.text
    endpoints = response.json()["endpoints"]
    assert "vsi OXY_1490" in endpoints["A"]["config"] and "  peer 10.5.5.5" in endpoints["B"]["config"]
    assert endpoints["A"]["vc_ids"][0]["vc_id"] == 1490


def test_should_require_vlan_when_building_l2vpn_from_hostnames(client, net):  # noqa: F811
    response = client.get("/api/configs/l2vpn", params={"a_device_id": net["dev"]["A-PE1"].id, "a_port": "ge-0/0/1"})
    assert response.status_code == 422


def test_should_list_existing_neighbours_with_their_hostname(db, client, make_service, net, monkeypatch):  # noqa: F811
    make_service("vrp", "l2vpn")
    net["dev"]["B-PE1"].loopback_ipv4 = "10.1.1.2"
    db.commit()
    monkeypatch.setattr(running_config, "fetch", lambda db, device_id: VRP)
    body = client.get("/api/configs/l2vpn/neighbours", params={"device_id": net["dev"]["A-PE1"].id}).json()
    by_ip = {row["ip"]: row for row in body["neighbours"]}
    assert by_ip["10.1.1.2"]["hostname"] == "B-PE1" and by_ip["10.1.1.2"]["pw_ids"] == [1480]
