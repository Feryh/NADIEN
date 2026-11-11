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
