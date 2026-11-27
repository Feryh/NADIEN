"""Description-based links that LLDP or the far end contradicts, and pairs approved by hand."""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import models as m
from app.database import get_db
from app.main import app
from app.services.topology import discover_links


def _network(db, far_description: str):
    """PAD-PAU-BNG1-M4 Eth-Trunk74 says it goes to KDR-PUW-BNG1-M4 Eth-Trunk1; KDR's trunk says `far_description`.
    MKS-BSW-ME3-M8 exists so 'mks' reads like an inventory hostname, as on the real network."""
    pop = m.Pop(code="SUM", name="Sumatera")
    db.add(pop)
    db.flush()
    pad, kdr, mks = (m.Device(hostname=h, pop_id=pop.id) for h in ("PAD-PAU-BNG1-M4", "KDR-PUW-BNG1-M4", "MKS-BSW-ME3-M8"))
    db.add_all([pad, kdr, mks])
    db.flush()
    db.add_all([
        m.Interface(device_id=pad.id, name="Eth-Trunk74", if_type="bundle", speed_gbps=100, oper_status="up",
                    description="to KDR-PUW-BNG1-M4:Eth-Trunk1 via ISAT IOH INDOSAT CID"),
        m.Interface(device_id=kdr.id, name="Eth-Trunk1", if_type="bundle", speed_gbps=10, oper_status="up",
                    description=far_description),
    ])
    db.commit()


def test_should_not_link_ports_when_far_port_description_points_to_another_city(db):
    _network(db, "to MKS-BSW-BNG2-M4:Eth-Trunk7.100 via INDOSAT ISAT IOH CID")
    assert discover_links(db).config_links == 0


def test_should_link_ports_when_far_port_description_is_empty(db):
    _network(db, "")
    assert discover_links(db).config_links == 1


def test_should_link_ports_when_far_port_description_names_a_box_in_the_same_city(db):
    _network(db, "to PAD-NDC-BNG2-M4:Eth-Trunk74")  # a renamed/replaced box in Padang, not a contradiction
    assert discover_links(db).config_links == 1


def test_should_remove_existing_config_link_when_far_end_now_contradicts_it(db):
    _network(db, "")
    discover_links(db)
    kdr_port = db.scalar(select(m.Interface).where(m.Interface.name == "Eth-Trunk1"))
    kdr_port.description = "to MKS-BSW-BNG2-M4:Eth-Trunk7.100 via INDOSAT ISAT IOH CID"
    db.commit()
    result = discover_links(db)
    assert result.superseded == 1 and db.scalars(select(m.Link)).all() == []


def _lldp_to_unknown_box(db, port_name: str = "25GE0/5/30") -> None:
    """KDR's trunk member sees, over LLDP, a box the inventory does not have (a carrier's ZTE: MAC only)."""
    kdr = db.scalar(select(m.Device).where(m.Device.hostname == "KDR-PUW-BNG1-M4"))
    trunk = db.scalar(select(m.Interface).where(m.Interface.device_id == kdr.id, m.Interface.name == "Eth-Trunk1"))
    db.add(m.Interface(device_id=kdr.id, name=port_name, if_type="physical", speed_gbps=10, oper_status="up",
                       parent_id=trunk.id))
    db.add(m.LldpNeighbor(device_id=kdr.id, local_interface=port_name, remote_system_name="744aa42a7fc8",
                          remote_port_id="xxvgei-1/1/0/12"))
    db.commit()


def test_should_not_pair_bundle_from_description_when_lldp_sees_an_unknown_box_on_its_member(db):
    _network(db, "")
    _lldp_to_unknown_box(db)
    assert discover_links(db).config_links == 0


def test_should_remove_existing_config_link_when_lldp_now_sees_an_unknown_box_there(db):
    _network(db, "")
    discover_links(db)
    _lldp_to_unknown_box(db)
    assert discover_links(db).superseded == 1 and db.scalars(select(m.Link)).all() == []


def test_should_list_port_whose_lldp_neighbour_is_unknown_when_its_bundle_names_an_inventory_device(db):
    from app.services.topology import unknown_lldp_peers

    _network(db, "to MKS-BSW-ME3-M8:Eth-Trunk7 via INDOSAT")
    _lldp_to_unknown_box(db)
    rows = unknown_lldp_peers(db)
    assert [(r["device"], r["port"], [x["neighbor"] for x in r["lldp"]], r["described_device"]) for r in rows] == [
        ("KDR-PUW-BNG1-M4", "Eth-Trunk1", ["744aa42a7fc8"], "MKS-BSW-ME3-M8")]


def test_should_pair_cell_site_uplink_when_its_mac_only_lldp_neighbour_reports_a_port_of_the_described_csr(db):
    pop = m.Pop(code="KDI", name="Kendari")
    db.add(pop)
    db.flush()
    bng, csr = m.Device(hostname="KDR-PUW-BNG1-M4", pop_id=pop.id), m.Device(hostname="SUL-SG-KDI-0612-ME1-ZTE", pop_id=pop.id)
    db.add_all([bng, csr])
    db.flush()
    trunk = m.Interface(device_id=bng.id, name="Eth-Trunk20", if_type="bundle", speed_gbps=10, oper_status="up",
                        description="to SUL-SG-KDI-0612-ME1-ZTE:smartgroup1")
    db.add(trunk)
    db.flush()
    db.add_all([
        m.Interface(device_id=bng.id, name="GigabitEthernet0/1/0", speed_gbps=10, oper_status="up", parent_id=trunk.id),
        m.Interface(device_id=csr.id, name="smartgroup1", if_type="bundle", speed_gbps=10, oper_status="up"),
        m.Interface(device_id=csr.id, name="xgei-1/1/0/9", speed_gbps=10, oper_status="up"),
        m.LldpNeighbor(device_id=bng.id, local_interface="GigabitEthernet0/1/0", remote_system_name="d80ae6a00af0",
                       remote_port_id="xgei-1/1/0/9"),
    ])
    db.commit()
    assert discover_links(db).config_links == 1


# --------------------------------------------------------------------------- manual approval


@pytest.fixture
def client(db):
    app.dependency_overrides[get_db] = lambda: db
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


def _blocked_pair(db) -> tuple[int, int]:
    """PAD Eth-Trunk74 <> KDR Eth-Trunk1, refused by discovery because LLDP on KDR's member sees an unknown box."""
    _network(db, "to PAD-PAU-BNG1-M4:Eth-Trunk74")
    _lldp_to_unknown_box(db)
    pad, kdr = (db.scalar(select(m.Interface).where(m.Interface.name == n)) for n in ("Eth-Trunk74", "Eth-Trunk1"))
    return pad.id, kdr.id


def test_should_list_blocked_port_with_its_described_peer_port_when_lldp_sees_an_unknown_box(db, client):
    pad, kdr = _blocked_pair(db)
    rows = client.get("/api/topology/lldp-unknown-peers").json()
    assert [(r["interface_id"], r["peer_interface_id"], r["approval"]) for r in rows] == [(kdr, pad, None)]


def test_should_draw_approved_links_and_keep_them_through_discovery_when_approved_together(db, client):
    pad, kdr = _blocked_pair(db)
    response = client.post("/api/topology/link-approvals", json={"pairs": [{"interface_id": kdr, "peer_interface_id": pad}]})
    assert response.status_code == 201 and response.json()["approved"] == 1
    discover_links(db)
    links = db.scalars(select(m.Link)).all()
    assert [(l.discovery_source, l.status) for l in links] == [("approved", "up")]


def test_should_approve_the_valid_pairs_and_report_the_refused_one_when_a_batch_mixes_them(db, client):
    pad, kdr = _blocked_pair(db)
    pairs = [{"interface_id": kdr, "peer_interface_id": 999_999}, {"interface_id": kdr, "peer_interface_id": pad}]
    body = client.post("/api/topology/link-approvals", json={"pairs": pairs}).json()
    assert (body["approved"], [r["peer_interface_id"] for r in body["rejected"]]) == (1, [999_999])
    assert len(db.scalars(select(m.LinkApproval)).all()) == 1


def test_should_answer_409_and_approve_nothing_when_no_pair_of_a_batch_can_be_approved(db, client):
    _, kdr = _blocked_pair(db)
    response = client.post("/api/topology/link-approvals", json={"pairs": [{"interface_id": kdr, "peer_interface_id": 999_999}]})
    assert response.status_code == 409 and db.scalars(select(m.LinkApproval)).all() == []


def test_should_remove_link_and_not_redraw_it_when_approval_is_revoked(db, client):
    pad, kdr = _blocked_pair(db)
    client.post("/api/topology/link-approvals", json={"pairs": [{"interface_id": kdr, "peer_interface_id": pad}]})
    approval = db.scalar(select(m.LinkApproval))
    assert client.delete(f"/api/topology/link-approvals/{approval.id}").status_code == 204
    discover_links(db)
    assert db.scalars(select(m.Link)).all() == []


def _replaced_box(db) -> int:
    """KDR's trunk still names MKS-BSW-BNG2-M4, replaced by MKS-BSW-BNG2-M8 (the only -BNG2- box at MKS-BSW)."""
    _network(db, "to MKS-BSW-BNG2-M4:Eth-Trunk7.100 via INDOSAT ISAT IOH CID")
    _lldp_to_unknown_box(db)
    m8 = m.Device(hostname="MKS-BSW-BNG2-M8", pop_id=db.scalar(select(m.Pop.id)))
    db.add(m8)
    db.flush()
    db.add(m.Interface(device_id=m8.id, name="Eth-Trunk7", if_type="bundle", speed_gbps=10, oper_status="up"))
    db.commit()
    return m8.id


def test_should_suggest_the_replacement_box_and_its_port_when_description_names_a_box_no_longer_in_inventory(db, client):
    _replaced_box(db)
    (row,) = client.get("/api/topology/lldp-unknown-peers").json()
    assert (row["described_device"], row["suggested_device"], row["peer_device"], row["peer_port"]) == (
        "MKS-BSW-BNG2-M4", "MKS-BSW-BNG2-M8", "MKS-BSW-BNG2-M8", "Eth-Trunk7")


def test_should_show_the_hand_picked_peer_when_row_was_approved_with_another_port(db, client):
    _replaced_box(db)
    (row,) = client.get("/api/topology/lldp-unknown-peers").json()
    pad = db.scalar(select(m.Interface).where(m.Interface.name == "Eth-Trunk74"))
    client.post("/api/topology/link-approvals", json={"pairs": [{"interface_id": row["interface_id"], "peer_interface_id": pad.id}]})
    (row,) = client.get("/api/topology/lldp-unknown-peers").json()
    assert (row["peer_device"], row["peer_port"], row["approval"] is not None) == ("PAD-PAU-BNG1-M4", "Eth-Trunk74", True)


def test_should_keep_both_links_when_two_sites_are_approved_to_the_same_hub_trunk(db, client):
    m8 = _replaced_box(db)
    hub = db.scalar(select(m.Interface).where(m.Interface.device_id == m8, m.Interface.name == "Eth-Trunk7"))
    kdr, pad = (db.scalar(select(m.Interface).where(m.Interface.name == n)) for n in ("Eth-Trunk1", "Eth-Trunk74"))
    pairs = [{"interface_id": kdr.id, "peer_interface_id": hub.id}, {"interface_id": pad.id, "peer_interface_id": hub.id}]
    assert client.post("/api/topology/link-approvals", json={"pairs": pairs}).status_code == 201
    discover_links(db)
    assert sorted(l.discovery_source for l in db.scalars(select(m.Link))) == ["approved", "approved"]


def test_should_suggest_the_full_device_when_description_writes_its_name_without_the_model_suffix(db, client):
    _network(db, "to MKS-BSW-BNG2:Eth-Trunk7 via INDOSAT")  # MKS-BSW-BNG2-M8 written without -M8
    _lldp_to_unknown_box(db)
    m8 = m.Device(hostname="MKS-BSW-BNG2-M8", pop_id=db.scalar(select(m.Pop.id)))
    db.add(m8)
    db.flush()
    db.add(m.Interface(device_id=m8.id, name="Eth-Trunk7", if_type="bundle", speed_gbps=10, oper_status="up"))
    db.commit()
    (row,) = client.get("/api/topology/lldp-unknown-peers").json()
    assert (row["suggested_device"], row["peer_port"]) == ("MKS-BSW-BNG2-M8", "Eth-Trunk7")


def test_should_name_the_link_that_already_uses_the_peer_port_when_listing_a_pair(db, client):
    pad, _ = _blocked_pair(db)
    other = m.Device(hostname="PAD-PAU-OLT1-ZTE", pop_id=db.scalar(select(m.Pop.id)))
    db.add(other)
    db.flush()
    olt_port = m.Interface(device_id=other.id, name="xgei-1/10/1", speed_gbps=10, oper_status="up")
    db.add(olt_port)
    db.flush()
    db.add(m.Link(name="PAD-PAU-BNG1-M4:Eth-Trunk74 <> PAD-PAU-OLT1-ZTE:xgei-1/10/1", a_device_id=db.get(m.Interface, pad).device_id,
                  a_interface_id=pad, b_device_id=other.id, b_interface_id=olt_port.id, discovery_source="lldp", status="up"))
    db.commit()
    (row,) = client.get("/api/topology/lldp-unknown-peers").json()
    assert row["taken_by"] == "PAD-PAU-BNG1-M4:Eth-Trunk74 <> PAD-PAU-OLT1-ZTE:xgei-1/10/1"


def _nni_hub(db) -> tuple[int, int]:
    """MKS-BSW-BNG2-M8 Eth-Trunk7 goes to a third-party NNI: LLDP links it to LSM-LSM-BNG1-M4 Eth-Trunk5, and KDR's
    trunk (one VLAN of the same NNI) names it too. Returns (KDR Eth-Trunk1, hub Eth-Trunk7)."""
    m8 = _replaced_box(db)
    pop = db.scalar(select(m.Pop.id))
    lsm = m.Device(hostname="LSM-LSM-BNG1-M4", pop_id=pop)
    db.add(lsm)
    db.flush()
    hub = db.scalar(select(m.Interface).where(m.Interface.device_id == m8, m.Interface.name == "Eth-Trunk7"))
    far = m.Interface(device_id=lsm.id, name="Eth-Trunk5", if_type="bundle", speed_gbps=100, oper_status="up")
    db.add(far)
    db.flush()
    db.add_all([
        m.Interface(device_id=m8, name="100GE1/1/1", speed_gbps=100, oper_status="up", parent_id=hub.id),
        m.Interface(device_id=lsm.id, name="100GE0/1/0", speed_gbps=100, oper_status="up", parent_id=far.id),
        m.LldpNeighbor(device_id=m8, local_interface="100GE1/1/1", remote_system_name="LSM-LSM-BNG1-M4",
                       remote_port_id="100GE0/1/0"),
    ])
    db.commit()
    discover_links(db)
    kdr = db.scalar(select(m.Interface).where(m.Interface.name == "Eth-Trunk1"))
    return kdr.id, hub.id


def test_should_refuse_a_plain_approval_onto_a_port_lldp_already_links(db, client):
    kdr, hub = _nni_hub(db)
    response = client.post("/api/topology/link-approvals", json={"pairs": [{"interface_id": kdr, "peer_interface_id": hub}]})
    assert response.status_code == 409 and "dipakai link lain" in response.json()["detail"]


def test_should_draw_and_keep_a_shared_approval_beside_the_lldp_link_when_the_port_is_an_nni(db, client):
    kdr, hub = _nni_hub(db)
    pair = {"interface_id": kdr, "peer_interface_id": hub, "shared": True}
    assert client.post("/api/topology/link-approvals", json={"pairs": [pair]}).status_code == 201
    discover_links(db)
    assert sorted(l.discovery_source for l in db.scalars(select(m.Link))) == ["approved", "lldp"]


# --------------------------------------------------------------------------- point-to-point /30 match


def _p2p_case(db) -> tuple[int, int]:
    """BGL Eth-Trunk16 (172.16.17.214/30) reaches BTM 100GE1/1/1.536 (.213/30) over a carrier: LLDP on BGL sees the
    carrier's box, and the description still names the replaced BTM-NIX-SPE1-M4. Returns (BGL trunk, BTM port)."""
    pop = m.Pop(code="BTM", name="Batam")
    db.add(pop)
    db.flush()
    bgl, btm = m.Device(hostname="BGL-GCP-BNG1-M4", pop_id=pop.id), m.Device(hostname="BTM-NIX-SPE1-X8", pop_id=pop.id)
    db.add_all([bgl, btm])
    db.flush()
    trunk = m.Interface(device_id=bgl.id, name="Eth-Trunk16", if_type="bundle", speed_gbps=100, oper_status="up",
                        ipv4_address="172.16.17.214/30", description="to BTM-NIX-SPE1-M4 100GE0/5/1.536 via LA Lintasarta")
    nni = m.Interface(device_id=btm.id, name="100GE1/1/1", speed_gbps=100, oper_status="up")
    db.add_all([trunk, nni])
    db.flush()
    db.add_all([
        m.Interface(device_id=bgl.id, name="100GE0/1/0", speed_gbps=100, oper_status="up", parent_id=trunk.id),
        m.Interface(device_id=btm.id, name="100GE1/1/1.536", if_type="subinterface", oper_status="up",
                    ipv4_address="172.16.17.213/30"),
        m.Interface(device_id=btm.id, name="100GE1/1/1.535", if_type="subinterface", oper_status="up",
                    ipv4_address="172.16.17.209/30"),
        m.LldpNeighbor(device_id=bgl.id, local_interface="100GE0/1/0", remote_system_name="LA-NNI-SW01",
                       remote_port_id="Ten1/0/7"),
    ])
    db.commit()
    return trunk.id, nni.id


def test_should_pair_the_port_carrying_the_same_30_when_lldp_sees_a_carrier_box(db, client):
    trunk, nni = _p2p_case(db)
    (row,) = client.get("/api/topology/lldp-unknown-peers").json()
    assert (row["interface_id"], row["peer_interface_id"], row["peer_device"]) == (trunk, nni, "BTM-NIX-SPE1-X8")
    assert row["ip_match"]["subnet"] == "172.16.17.212/30" and row["ip_match"]["remote_port"] == "100GE1/1/1.536"


def test_should_not_pair_by_address_when_the_subnet_is_wider_than_31(db, client):
    _p2p_case(db)
    for iface in db.scalars(select(m.Interface).where(m.Interface.ipv4_address.is_not(None))):
        iface.ipv4_address = iface.ipv4_address.replace("/30", "/29")
    db.commit()
    (row,) = client.get("/api/topology/lldp-unknown-peers").json()
    assert row["ip_match"] is None and row["suggested_device"] == "BTM-NIX-SPE1-X8"  # the description guess only


def test_should_draw_the_link_of_a_shared_30_when_discovery_runs(db):
    trunk, nni = _p2p_case(db)
    result = discover_links(db)
    links = [(l.a_interface_id, l.b_interface_id, l.discovery_source) for l in db.scalars(select(m.Link))]
    assert result.ip_links == 1 and [(set(l[:2]), l[2]) for l in links] == [({trunk, nni}, "ip")]


def test_should_leave_the_30_alone_when_lldp_links_its_port_to_another_inventory_device(db):
    trunk, _ = _p2p_case(db)
    member = db.scalar(select(m.Interface).where(m.Interface.parent_id == trunk))
    other = m.Device(hostname="BGL-GCP-ME1-M8", pop_id=db.scalar(select(m.Pop.id)))
    db.add(other)
    db.flush()
    db.add(m.Interface(device_id=other.id, name="100GE0/2/0", speed_gbps=100, oper_status="up"))
    lldp = db.scalar(select(m.LldpNeighbor).where(m.LldpNeighbor.local_interface == member.name))
    lldp.remote_system_name, lldp.remote_port_id = "BGL-GCP-ME1-M8", "100GE0/2/0"
    db.commit()
    discover_links(db)
    assert [l.discovery_source for l in db.scalars(select(m.Link))] == ["lldp"]


def test_should_drop_the_address_pair_link_when_one_end_loses_its_address(db):
    _p2p_case(db)
    discover_links(db)
    db.scalar(select(m.Interface).where(m.Interface.name == "100GE1/1/1.536")).ipv4_address = None
    db.commit()
    assert discover_links(db).superseded == 1 and db.scalars(select(m.Link)).all() == []


def test_should_drop_the_row_from_the_unknown_lldp_list_once_discovery_draws_its_30(db, client):
    _p2p_case(db)
    discover_links(db)
    assert client.get("/api/topology/lldp-unknown-peers").json() == []


def test_should_keep_the_described_switch_links_when_a_30_crosses_that_l2_switch(db):
    """PEIN Eth-Trunk30 and PE1 Eth-Trunk10 both describe the DS1 switch between them; their /30 is an L3 hop
    across it, not a cable, so the two described links stay and no address-pair link is drawn."""
    pop = m.Pop(code="SBY", name="Surabaya")
    db.add(pop)
    db.flush()
    pein, pe, ds = (m.Device(hostname=h, pop_id=pop.id) for h in ("PEIN1-H-SBY-NIX", "PE1-ATN910-SBY-NIX", "DS1-S6730-SBY-NIX"))
    db.add_all([pein, pe, ds])
    db.flush()
    db.add_all([
        m.Interface(device_id=pein.id, name="Eth-Trunk30", if_type="bundle", oper_status="up", ipv4_address="10.9.0.1/30",
                    description="to DS1-S6730-SBY-NIX Eth-Trunk30"),
        m.Interface(device_id=pe.id, name="Eth-Trunk10", if_type="bundle", oper_status="up", ipv4_address="10.9.0.2/30",
                    description="to DS1-S6730-SBY-NIX Eth-Trunk31"),
        m.Interface(device_id=ds.id, name="Eth-Trunk30", if_type="bundle", oper_status="up", description="to PEIN1-H-SBY-NIX Eth-Trunk30"),
        m.Interface(device_id=ds.id, name="Eth-Trunk31", if_type="bundle", oper_status="up", description="to PE1-ATN910-SBY-NIX Eth-Trunk10"),
    ])
    db.commit()
    result = discover_links(db)
    assert (result.config_links, result.ip_links) == (2, 0)


def test_should_draw_the_address_pair_with_the_holder_that_is_up_when_a_standby_box_keeps_the_same_30(db):
    """DPS-KRS TenGigE0/0/0/3.948 (.146/30) reaches DPS-NDC-ME1 Eth-Trunk4.948 (.145/30); DPS-NDC-BNG2 still has .145
    configured on a trunk that is down. Only the two holders that are up are linked."""
    pop = m.Pop(code="DPS", name="Denpasar")
    db.add(pop)
    db.flush()
    krs, me1, bng2 = (m.Device(hostname=h, pop_id=pop.id)
                      for h in ("DPS-KRS-ME1-NCS540", "DPS-NDC-ME1-M8", "DPS-NDC-BNG2-M8"))
    db.add_all([krs, me1, bng2])
    db.flush()
    ports = {}
    for device, port, sub, ip, oper in ((krs, "TenGigE0/0/0/3", ".948", "172.16.16.146/30", "up"),
                                        (me1, "Eth-Trunk4", ".948", "172.16.16.145/30", "up"),
                                        (bng2, "Eth-Trunk20", ".948", "172.16.16.145/30", "down")):
        ports[device.hostname] = m.Interface(device_id=device.id, name=port, if_type="bundle" if "Trunk" in port
                                             else "physical", speed_gbps=10, oper_status=oper)
        db.add_all([ports[device.hostname],
                    m.Interface(device_id=device.id, name=port + sub, if_type="subinterface", oper_status=oper,
                                ipv4_address=ip)])
    db.commit()
    assert discover_links(db).ip_links == 1
    (link,) = db.scalars(select(m.Link)).all()
    assert {link.a_interface_id, link.b_interface_id} == {ports["DPS-KRS-ME1-NCS540"].id, ports["DPS-NDC-ME1-M8"].id}


def test_should_not_draw_a_link_from_the_lldp_table_of_a_box_observium_stopped_polling(db):
    """SRG-SMY-ME1-FH820 was swapped for SRG-SMY-ME1-M8: Observium no longer polls it, so its LLDP table stays as it
    was on the day it dropped out, while SRG-BNG-HW-M4's own table now names the M8."""
    from datetime import datetime, timedelta

    pop = m.Pop(code="SRG", name="Serang")
    db.add(pop)
    db.flush()
    bng, old, new = (m.Device(hostname=h, pop_id=pop.id)
                     for h in ("SRG-BNG-HW-M4", "SRG-SMY-ME1-FH820", "SRG-SMY-ME1-M8"))
    db.add_all([bng, old, new])
    db.flush()
    db.add_all([m.Interface(device_id=bng.id, name="100GE0/5/1", speed_gbps=100),
                m.Interface(device_id=old.id, name="eth-100gi0/1/0/25", speed_gbps=100),
                m.Interface(device_id=new.id, name="100GE0/2/0", speed_gbps=100)])
    now = datetime(2026, 11, 23, 2, 0)
    db.add_all([
        m.LldpNeighbor(device_id=bng.id, local_interface="100GE0/5/1", remote_system_name="SRG-SMY-ME1-M8",
                       remote_port_id="100GE0/2/0", collected_at=now),
        m.LldpNeighbor(device_id=old.id, local_interface="eth-100gi0/1/0/25", remote_system_name="SRG-BNG-HW-M4",
                       remote_port_id="100GE0/5/1", collected_at=now - timedelta(days=21)),
    ])
    db.commit()
    discover_links(db)
    assert [l.name for l in db.scalars(select(m.Link)) if l.status == "up"] == [
        "SRG-BNG-HW-M4:100GE0/5/1 <> SRG-SMY-ME1-M8:100GE0/2/0"]


def _olt_behind_dead_port(db, down_days: float):
    """SRG-NDC-SW1-NX93 Ethernet1/26: down since August 2025, yet Observium still lists the OLT on it, and the OLT's
    own (up) port still names the switch."""
    from datetime import datetime, timedelta, timezone

    pop = m.Pop(code="SRG", name="Serang")
    db.add(pop)
    db.flush()
    sw, olt = m.Device(hostname="SRG-NDC-SW1-NX93", pop_id=pop.id), m.Device(hostname="SRG-BTL-OLT1-ZTE", pop_id=pop.id)
    db.add_all([sw, olt])
    db.flush()
    now = datetime.now(timezone.utc)
    sw_port = m.Interface(device_id=sw.id, name="Ethernet1/26", speed_gbps=10, oper_status="down",
                          oper_changed_at=now - timedelta(days=down_days))
    olt_port = m.Interface(device_id=olt.id, name="xgei-1/11/1", speed_gbps=10, oper_status="up",
                           oper_changed_at=now - timedelta(days=21))
    db.add_all([sw_port, olt_port])
    db.add_all([
        m.LldpNeighbor(device_id=sw.id, local_interface="Ethernet1/26", remote_system_name="SRG-BTL-OLT1-ZTE",
                       remote_port_id="xgei-1/11/1", collected_at=now),
        m.LldpNeighbor(device_id=olt.id, local_interface="xgei-1/11/1", remote_system_name="SRG-NDC-SW1-NX93",
                       remote_port_id="Ethernet1/26", collected_at=now),
    ])
    db.commit()
    return sw_port


def test_should_not_draw_an_lldp_link_on_a_port_down_past_the_lldp_age_limit(db):
    _olt_behind_dead_port(db, down_days=477)
    discover_links(db)
    assert db.scalars(select(m.Link)).all() == []


def test_should_keep_an_lldp_link_on_a_port_that_went_down_recently(db):
    _olt_behind_dead_port(db, down_days=1)
    discover_links(db)
    assert len(db.scalars(select(m.Link)).all()) == 1


def test_should_remove_a_known_link_once_its_port_has_been_down_past_the_lldp_age_limit(db):
    from datetime import timedelta

    port = _olt_behind_dead_port(db, down_days=1)
    discover_links(db)
    port.oper_changed_at -= timedelta(days=476)
    db.commit()
    assert discover_links(db).superseded == 1 and db.scalars(select(m.Link)).all() == []


def test_should_judge_an_unreported_member_by_its_long_down_bundle_when_only_the_far_end_lists_it(db):
    from datetime import datetime, timedelta, timezone

    pop = m.Pop(code="SRG", name="Serang")
    db.add(pop)
    db.flush()
    sw, olt = m.Device(hostname="SRG-NDC-SW1-NX93", pop_id=pop.id), m.Device(hostname="SRG-SRG-OLT1-ZTE", pop_id=pop.id)
    db.add_all([sw, olt])
    db.flush()
    now = datetime.now(timezone.utc)
    bundle = m.Interface(device_id=sw.id, name="port-channel14", if_type="bundle", oper_status="down",
                         oper_changed_at=now - timedelta(days=477))
    db.add(bundle)
    db.flush()
    db.add_all([m.Interface(device_id=sw.id, name="Ethernet1/5", parent_id=bundle.id, oper_status="up"),
                m.Interface(device_id=olt.id, name="xgei-1/10/1", speed_gbps=10, oper_status="up",
                            oper_changed_at=now - timedelta(days=21)),
                m.LldpNeighbor(device_id=olt.id, local_interface="xgei-1/10/1", remote_system_name="SRG-NDC-SW1-NX93",
                               remote_port_id="Ethernet1/5", collected_at=now)])
    db.commit()
    discover_links(db)
    assert db.scalars(select(m.Link)).all() == []


def _pair_with_one_dead_port(db, **dead_port: object):
    """Two boxes whose ports still share a /30, one of the ports down since 2025."""
    from datetime import datetime, timedelta, timezone

    pop = m.Pop(code="SMG", name="Semarang")
    db.add(pop)
    db.flush()
    sw, olt = m.Device(hostname="SMG-GMK-SW1-NX93", pop_id=pop.id), m.Device(hostname="SMG-MJN-OLT1-HW", pop_id=pop.id)
    db.add_all([sw, olt])
    db.flush()
    now = datetime.now(timezone.utc)
    db.add_all([
        m.Interface(device_id=sw.id, name="Ethernet1/28", speed_gbps=10, oper_status="down",
                    oper_changed_at=now - timedelta(days=440), **dead_port),
        m.Interface(device_id=olt.id, name="ethernet0/9/0", speed_gbps=10, oper_status="up",
                    oper_changed_at=now - timedelta(days=28), description="to SMG-GMK-SW1-NX93 Ethernet1/28",
                    ipv4_address=dead_port.get("ipv4_address") and "10.9.9.2/30"),
    ])
    db.commit()


def test_should_not_link_a_slash_30_whose_port_has_been_down_past_the_lldp_age_limit(db):
    _pair_with_one_dead_port(db, ipv4_address="10.9.9.1/30")
    discover_links(db)
    assert db.scalars(select(m.Link)).all() == []


def test_should_not_draw_links_of_a_decommissioned_device_from_its_config(db):
    _network(db, "")
    links = discover_links(db).config_links
    device = db.scalars(select(m.Device)).first()
    device.status = "decommissioned"
    db.commit()
    assert links == 1 and discover_links(db).config_links == 0
    assert all(l.status == "down" for l in db.scalars(select(m.Link)))



def _olt_chain(db) -> None:
    """GTO-KTT-BNG1-M4 Eth-Trunk5 goes to GTO-TLB-OLT1-HW (not polled); GTO-TLB-OLT2-HW, behind it, serves cell site
    SUL-GO-GTL-0086-ME1-ZTE. Someone approved the trunk onto the cell site's port (they share Eth-Trunk5.575)."""
    pop = m.Pop(code="GTO", name="Gorontalo")
    db.add(pop)
    db.flush()
    bng, olt2, csr = (m.Device(hostname=h, pop_id=pop.id) for h in ("GTO-KTT-BNG1-M4", "GTO-TLB-OLT2-HW", "SUL-GO-GTL-0086-ME1-ZTE"))
    db.add_all([bng, olt2, csr])
    db.flush()
    trunk = m.Interface(device_id=bng.id, name="Eth-Trunk5", if_type="bundle", speed_gbps=40, oper_status="up",
                        description="to_olt GTO-TLB-OLT1-HW")
    site = m.Interface(device_id=csr.id, name="xgei-1/1/0/9", speed_gbps=10, oper_status="up")
    db.add_all([trunk, site, m.Interface(device_id=olt2.id, name="ethernet0/9/2", speed_gbps=10, oper_status="up")])
    db.flush()
    for k, port in enumerate(("25GE0/5/25", "25GE0/5/26")):
        db.add(m.Interface(device_id=bng.id, name=port, speed_gbps=10, oper_status="up", parent_id=trunk.id))
        db.add(m.LldpNeighbor(device_id=bng.id, local_interface=port, remote_system_name="GTO-TLB-OLT1-HW",
                              remote_port_id=f"ethernet0/8/{k}"))
    db.add(m.LldpNeighbor(device_id=csr.id, local_interface="xgei-1/1/0/9", remote_system_name="GTO-TLB-OLT2-HW",
                          remote_port_id="ethernet0/9/2"))
    db.add(m.LinkApproval(a_interface_id=trunk.id, b_interface_id=site.id, approved_by="admin", shared=True))
    db.commit()


def _link_names(db) -> list[str]:
    return sorted(f"{l.name} {l.discovery_source}" for l in db.scalars(select(m.Link)))


def test_should_link_the_trunk_to_the_box_its_lldp_names_when_observium_does_not_poll_that_box(db):
    _olt_chain(db)
    discover_links(db)
    olt1 = db.scalar(select(m.Device).where(m.Device.hostname == "GTO-TLB-OLT1-HW"))
    assert olt1 is not None and olt1.lldp_only
    assert [n for n in _link_names(db) if "OLT1" in n] == [
        "GTO-KTT-BNG1-M4:25GE0/5/25 <> GTO-TLB-OLT1-HW:ethernet0/8/0 lldp",
        "GTO-KTT-BNG1-M4:25GE0/5/26 <> GTO-TLB-OLT1-HW:ethernet0/8/1 lldp"]


def test_should_not_draw_an_approved_link_when_lldp_links_both_its_ports_to_other_boxes(db):
    _olt_chain(db)
    discover_links(db)
    assert not any("approved" in n for n in _link_names(db))


def test_should_not_create_a_device_for_a_carrier_box_at_a_site_the_inventory_lacks(db):
    _network(db, "")
    kdr = db.scalar(select(m.Device).where(m.Device.hostname == "KDR-PUW-BNG1-M4"))
    db.add(m.LldpNeighbor(device_id=kdr.id, local_interface="Eth-Trunk1", remote_system_name="JK-KYB-04-PE1-CSFCB1",
                          remote_port_id="Hu0/1/0/5"))
    db.commit()
    discover_links(db)
    assert db.scalar(select(m.Device).where(m.Device.hostname == "JK-KYB-04-PE1-CSFCB1")) is None
