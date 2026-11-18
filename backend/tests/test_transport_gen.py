"""End-to-end transport generator: SR-MPLS, SRv6 and VXLAN between two PEs."""

import pytest

from app.services.running_config import RunningConfigError
from app.services.transport_gen import TransportError, TransportIn, generate_transport
from tests.test_configgen import _vendor, client  # noqa: F401  (fixtures)
from tests.test_validation import net  # noqa: F401  (shared A — P — B network fixture)

XR_RUNNING = """router isis CORE
 net 49.0001.0100.0000.0001.00
 interface Loopback0
  passive
 !
 interface HundredGigE0/0/0/1
  address-family ipv4 unicast
 !
!
router bgp 46038
!
interface Loopback0
 ipv4 address 10.0.0.11 255.255.255.255
 ipv6 address 2001:db8::11/128
!
"""

VRP_RUNNING = """isis 1
 is-level level-2
 network-entity 49.0001.0100.0000.0002.00
#
bgp 46038
#
interface LoopBack0
 ip address 10.0.0.12 255.255.255.255
 isis prefix-sid index 120
#
"""


@pytest.fixture
def pes(db, net):  # noqa: F811
    def setup(a_dialect: str = "iosxr", b_dialect: str = "vrp", p_dialect: str = "iosxr"):
        for name, dialect, loopback in (("A-PE1", a_dialect, "10.0.0.11"), ("B-PE1", b_dialect, "10.0.0.12"),
                                        ("P1", p_dialect, "10.0.0.21")):
            device = net["dev"][name]
            device.vendor_id = _vendor(db, dialect).id
            device.loopback_ipv4 = loopback
        db.commit()
        return net["dev"]
    return setup


def _source(dev, running: dict[str, str]):
    by_id = {device.id: running.get(name, "") for name, device in dev.items()}
    return lambda device_id: by_id[device_id]


def _run(db, dev, running: dict[str, str] | None = None, transit: bool = False, **fields):
    payload = TransportIn(a_device_id=dev["A-PE1"].id, b_device_id=dev["B-PE1"].id,
                          transit_device_ids=[dev["P1"].id] if transit else [], **fields)
    running = running if running is not None else {"A-PE1": XR_RUNNING, "B-PE1": VRP_RUNNING}
    result = generate_transport(db, payload, _source(dev, running))
    return {d["role"] if d["role"] != "P" else d["device"]: d for d in result["devices"]}


def test_should_enable_sr_mpls_with_prefix_sid_on_the_iosxr_loopback(db, pes):
    dev = pes()
    a = _run(db, dev, technology="sr-mpls", sid_indexes={dev["A-PE1"].id: 11})["A"]
    start = a["config"].index(" interface Loopback0")
    assert a["config"][start:start + 3] == [" interface Loopback0", "  address-family ipv4 unicast", "   prefix-sid index 11"]


def test_should_keep_the_existing_prefix_sid_and_skip_the_underlay_when_sr_is_already_on(db, pes):
    b = _run(db, pes(), technology="sr-mpls")["B"]
    assert not any("prefix-sid" in line for line in b["config"]) and "index 120" in b["notes"][0]


def test_should_build_an_explicit_segment_list_through_the_transit_node_labels(db, pes):
    dev = pes()
    devices = _run(db, dev, transit=True, technology="sr-mpls", sid_indexes={dev["A-PE1"].id: 11, dev["P1"].id: 21})
    assert ["   index 10 mpls label 16021", "   index 20 mpls label 16120"] == [
        line for line in devices["A"]["config"] if "mpls label" in line]


def test_should_point_the_vrp_policy_back_to_pe_a_with_the_colour(db, pes):
    dev = pes()
    b = _run(db, dev, technology="sr-mpls", color=200, sid_indexes={dev["A-PE1"].id: 11})["B"]
    assert any(line.startswith(" sr-te policy NADIEN_C200_A_PE1 endpoint 10.0.0.11 color 200") for line in b["config"])


def test_should_reject_two_nodes_with_the_same_prefix_sid_index(db, pes):
    dev = pes()
    with pytest.raises(TransportError, match="lebih dari satu"):
        _run(db, dev, technology="sr-mpls", sid_indexes={dev["A-PE1"].id: 120})


def test_should_reject_an_index_outside_the_srgb(db, pes):
    dev = pes()
    with pytest.raises(TransportError, match="di luar SRGB"):
        _run(db, dev, technology="sr-mpls", srgb_end=16099, sid_indexes={dev["A-PE1"].id: 11})


def test_should_derive_the_srv6_locator_from_the_block_and_device_id(db, pes):
    dev = pes()
    a = _run(db, dev, technology="srv6", srv6_block="fcbb:bb00::/32")["A"]
    assert f"    prefix fcbb:bb00:{dev['A-PE1'].id:x}::/48" in a["config"]


def test_should_peer_bgp_vpnv4_over_srv6_to_the_ipv6_loopback_read_from_rancid(db, pes):
    dev = pes(a_dialect="vrp", b_dialect="iosxr")
    running = {"A-PE1": VRP_RUNNING, "B-PE1": XR_RUNNING}
    a = _run(db, dev, running, technology="srv6", vrf="CUST")["A"]
    assert "  peer 2001:db8::11 prefix-sid" in a["config"] and "  segment-routing ipv6 locator NADIEN" in a["config"]


def test_should_bind_the_vni_to_a_bridge_domain_on_the_vrp_access_port(db, pes):
    b = _run(db, pes(), technology="vxlan", vlan=1480, b_port="GigabitEthernet0/3/1")["B"]
    assert ["interface GigabitEthernet0/3/1.1480 mode l2", " encapsulation dot1q vid 1480", " bridge-domain 1480"] == \
        b["config"][b["config"].index("interface GigabitEthernet0/3/1.1480 mode l2"):][:3]
    assert " vxlan vni 11480" in b["config"]


def test_should_list_the_peer_vtep_when_vxlan_replication_is_static_on_vrp(db, pes):
    b = _run(db, pes(), technology="vxlan", vlan=1480, control_plane="static")["B"]
    assert " vni 11480 head-end peer-list 10.0.0.11" in b["config"] and not any("bgp" in l for l in b["config"])


def test_should_give_no_config_and_a_warning_when_the_vendor_has_no_template(db, pes):
    b = _run(db, pes(b_dialect="zte"), technology="srv6")["B"]
    assert b["config"] == [] and "belum tersedia" in b["warnings"][-1]


def test_should_warn_and_use_defaults_when_the_running_config_cannot_be_read(db, pes):
    dev = pes()

    def failing(_device_id: int) -> str:
        raise RunningConfigError("tidak ada backup")

    payload = TransportIn(technology="sr-mpls", a_device_id=dev["A-PE1"].id, b_device_id=dev["B-PE1"].id)
    a = generate_transport(db, payload, failing)["devices"][0]
    assert "router isis 1" in a["config"] and "tidak ada backup" in a["warnings"][0]


def test_should_answer_422_when_pe_a_and_b_are_the_same_device(client, pes):  # noqa: F811
    dev = pes()
    response = client.post("/api/configs/transport", json={"technology": "vxlan", "a_device_id": dev["A-PE1"].id,
                                                            "b_device_id": dev["A-PE1"].id, "vlan": 10})
    assert response.status_code == 422 and "berbeda" in response.json()["detail"]


def test_should_only_add_the_family_and_keep_the_session_on_rollback_when_the_bgp_peer_exists(db, pes):
    running = {"A-PE1": XR_RUNNING.replace("router bgp 46038\n", "router bgp 46038\n neighbor 10.0.0.12\n  remote-as 46038\n"),
               "B-PE1": VRP_RUNNING}
    a = _run(db, pes(), running, technology="vxlan", vlan=1480)["A"]
    assert "  remote-as 46038" not in a["config"] and " no neighbor 10.0.0.12" not in a["rollback"]
    assert "  no address-family l2vpn evpn" in a["rollback"]


def test_should_leave_out_the_commit_when_a_transit_node_needs_no_vxlan_config(db, pes):
    p = _run(db, pes(), transit=True, technology="vxlan", vlan=1480)["P1"]
    assert p["config"] == [] and p["commit"] == []


def test_should_cross_the_vpws_service_ids_between_the_two_pes(db, pes):
    devices = _run(db, pes(), technology="evpn", vlan=1480, a_port="Hu0/0/0/5", b_port="GE0/3/1")
    assert "   neighbor evpn evi 1480 target 2 source 1" in devices["A"]["config"]
    assert " local-service-id 2 remote-service-id 1" in devices["B"]["config"]


def test_should_bind_the_bridge_domain_to_the_evpn_instance_when_the_service_is_elan(db, pes):
    b = _run(db, pes(), technology="evpn", evpn_type="elan", vlan=1480, evi=500, b_port="GE0/3/1")["B"]
    assert ["evpn vpn-instance EVPN-500 bd-mode", "bridge-domain 1480", " evpn binding vpn-instance EVPN-500"] == [
        line for line in b["config"] if "EVPN-500" in line or line == "bridge-domain 1480"]


def test_should_use_the_evi_in_the_route_target_shared_by_both_pes(db, pes):
    devices = _run(db, pes(), technology="evpn", vlan=1480)
    assert "   route-target import 46038:1480" in devices["A"]["config"]
    assert " vpn-target 46038:1480 import-extcommunity" in devices["B"]["config"]
