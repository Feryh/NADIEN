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


def _bks(db):
    """BKS-NBK-ME1-6120HS smartgroup4 (2x100G) to BKS-LBJ-ME1-M8 Eth-Trunk3, neither seen by LLDP; the M8's
    members carry no bundle (its config is not in RANCID) and a retired 10G port still describes NBK."""
    pop = m.Pop(code="BKS", name="Bekasi")
    db.add(pop)
    db.flush()
    nbk = m.Device(hostname="BKS-NBK-ME1-6120HS", pop_id=pop.id, role="agg")
    lbj = m.Device(hostname="BKS-LBJ-ME1-M8", pop_id=pop.id, role="agg")
    db.add_all([nbk, lbj])
    db.flush()
    sg4 = m.Interface(device_id=nbk.id, name="smartgroup4", if_type="bundle", speed_gbps=200, oper_status="up",
                      description="Link to BKS-LBJ-ME1-M8")
    db.add(sg4)
    db.flush()
    db.add_all([
        m.Interface(device_id=nbk.id, name="cgei-1/1/0/35", speed_gbps=100, oper_status="up", parent_id=sg4.id,
                    description="link to BKS-LBJ-ME1-M8 via ONNET Main"),
        m.Interface(device_id=lbj.id, name="GigabitEthernet0/8/0", speed_gbps=10, oper_status="down",
                    description="to BKS-NBK-ME1-6120HS:cgei-1/1/0/36"),
        m.Interface(device_id=lbj.id, name="Eth-Trunk3", if_type="bundle", speed_gbps=200, oper_status="up",
                    description="to BKS-NBK-ME1-6120HS"),
        m.Interface(device_id=lbj.id, name="Eth-Trunk4", if_type="bundle", oper_status="down",
                    description="to BKS-NBK-ME1-6120HS"),
    ])
    db.commit()
    return nbk, lbj


def test_should_pair_live_bundles_when_descriptions_alone_name_the_other_device(db):
    _bks(db)
    discover_links(db)
    link = db.scalar(select(m.Link))
    assert (link.name, link.status, link.capacity_gbps) == (
        "BKS-NBK-ME1-6120HS:smartgroup4 <> BKS-LBJ-ME1-M8:Eth-Trunk3", "up", 200)


def test_should_drop_an_earlier_config_pairing_when_its_bundle_now_carries_another_link(db):
    nbk, lbj = _bks(db)
    member = db.scalar(select(m.Interface).where(m.Interface.name == "cgei-1/1/0/35"))
    stale_port = db.scalar(select(m.Interface).where(m.Interface.name == "GigabitEthernet0/8/0"))
    db.add(m.Link(name="old", a_device_id=nbk.id, a_interface_id=member.id, b_device_id=lbj.id,
                  b_interface_id=stale_port.id, capacity_gbps=10, status="down", discovery_source="config"))
    db.commit()
    discover_links(db)
    assert [link.name for link in db.scalars(select(m.Link))] == ["BKS-NBK-ME1-6120HS:smartgroup4 <> BKS-LBJ-ME1-M8:Eth-Trunk3"]


def test_should_not_take_a_shorter_port_name_inside_a_longer_one_as_named():
    from app.services.topology import _Discovery

    def port(id_: int, device: int, name: str, description: str) -> m.Interface:
        return m.Interface(id=id_, device_id=device, name=name, description=description, speed_gbps=100, oper_status="up")

    ours = [port(1, 1, "100GE1/0/1", "to B")]
    theirs = [port(2, 2, "100GE1/0/9", "to A:100GE1/0/14"), port(3, 2, "100GE1/0/8", "to A:100GE1/0/1")]
    assert [(a.id, b.id) for a, b in _Discovery.best_config_pairs(ours, theirs)] == [(1, 3)]
