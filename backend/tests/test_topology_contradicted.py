"""A port described by only one end, whose far port's own description points to another city."""

from sqlalchemy import select

from app import models as m
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
    assert [(r["device"], r["port"], r["bundle"], r["lldp_neighbor"], r["described_device"]) for r in rows] == [
        ("KDR-PUW-BNG1-M4", "25GE0/5/30", "Eth-Trunk1", "744aa42a7fc8", "MKS-BSW-ME3-M8")]


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
