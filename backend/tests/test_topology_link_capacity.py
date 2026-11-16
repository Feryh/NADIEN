"""Capacity of a discovered link when one end's speed is unknown."""

from sqlalchemy import select

from app import models as m
from app.services.topology import discover_links


def test_should_take_the_known_end_speed_when_the_far_port_reports_none(db):
    """BGR-WBG-OLT6-FH SFP+ 9/1 (10G) to BGR-WBG-ME1-6120HS xxvgei-1/1/0/24, a port the NMS does not report."""
    pop = m.Pop(code="BGR", name="Bogor")
    db.add(pop)
    db.flush()
    olt = m.Device(hostname="BGR-WBG-OLT6-FH", pop_id=pop.id, role="olt")
    me = m.Device(hostname="BGR-WBG-ME1-6120HS", pop_id=pop.id, role="agg")
    db.add_all([olt, me])
    db.flush()
    db.add_all([m.Interface(device_id=olt.id, name="SFP+ 9/1", speed_gbps=10, oper_status="up"),
                m.Interface(device_id=me.id, name="xxvgei-1/1/0/24", speed_gbps=None, oper_status="up"),
                m.LldpNeighbor(device_id=olt.id, local_interface="SFP+ 9/1", remote_system_name=me.hostname,
                               remote_port_id="xxvgei-1/1/0/24")])
    db.commit()
    discover_links(db)
    assert db.scalar(select(m.Link)).capacity_gbps == 10
