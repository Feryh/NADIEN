"""LLDP that names a logical interface (VLAN interface, management port) as the far port draws no link."""

from sqlalchemy import select

from app import models as m
from app.services.topology import discover_links


def _olt_and_pe(db, pe_port_description: str | None = None) -> tuple[m.Interface, m.Interface]:
    """OLT2-C300-PBR-SKJ xgei_1/19/1, whose LLDP neighbour PEIN1-H-PBR-SKJ announces Vbdif59 (a VLAN interface)."""
    pop = m.Pop(code="PBR", name="Pekanbaru")
    db.add(pop)
    db.flush()
    olt = m.Device(hostname="OLT2-C300-PBR-SKJ", pop_id=pop.id, role="olt")
    pe = m.Device(hostname="PEIN1-H-PBR-SKJ", pop_id=pop.id, role="pe")
    db.add_all([olt, pe])
    db.flush()
    uplink = m.Interface(device_id=olt.id, name="xgei_1/19/1", speed_gbps=10, oper_status="up",
                         description="TO-PEIN1-H-PBR-SKJ-GE0/1/1")
    vbdif = m.Interface(device_id=pe.id, name="Vbdif59", if_type="virtual", speed_gbps=1, oper_status="up")
    pe_port = m.Interface(device_id=pe.id, name="GE0/1/1", speed_gbps=10, oper_status="up", description=pe_port_description)
    db.add_all([uplink, vbdif, pe_port,
                m.LldpNeighbor(device_id=olt.id, local_interface="xgei_1/19/1", remote_system_name=pe.hostname,
                               remote_port_id="Vbdif59")])
    db.commit()
    return uplink, pe_port


def test_should_draw_no_link_to_a_vlan_interface_when_lldp_announces_it_as_the_far_port(db):
    _olt_and_pe(db)
    assert discover_links(db).lldp_links == 0


def test_should_pair_the_physical_port_the_description_names_when_lldp_only_gives_a_vlan_interface(db):
    uplink, pe_port = _olt_and_pe(db)
    discover_links(db)
    (link,) = db.scalars(select(m.Link)).all()
    assert {link.a_interface_id, link.b_interface_id} == {uplink.id, pe_port.id} and link.discovery_source == "config"


def test_should_remove_an_existing_link_that_ends_on_a_vlan_interface(db):
    uplink, _ = _olt_and_pe(db)
    vbdif = db.scalar(select(m.Interface).where(m.Interface.name == "Vbdif59"))
    db.add(m.Link(name="old", a_device_id=uplink.device_id, a_interface_id=uplink.id, b_device_id=vbdif.device_id,
                  b_interface_id=vbdif.id, capacity_gbps=1, discovery_source="lldp"))
    db.commit()
    assert discover_links(db).superseded == 1
    assert db.scalar(select(m.Link).where(m.Link.b_interface_id == vbdif.id)) is None
