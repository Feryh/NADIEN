"""A bundle has one LACP partner per device: when its members' LLDP points to two bundles, most members win."""

from sqlalchemy import select

from app import models as m
from app.services.topology import discover_links


def _bng_and_me(db, stray: bool = True, me_members: int = 3) -> None:
    """BDG-BBU-BNG2 smartgroup2 (3x100G) to BDG-BBU-ME1-M8 Eth-Trunk2; the NMS files one member's neighbour under
    cgei-0/2/1/8, a member of the retired smartgroup99."""
    pop = m.Pop(code="BDG", name="Bandung")
    db.add(pop)
    db.flush()
    bng = m.Device(hostname="BDG-BBU-BNG2-M6K-8SP", pop_id=pop.id, role="bng")
    me = m.Device(hostname="BDG-BBU-ME1-M8", pop_id=pop.id, role="agg")
    db.add_all([bng, me])
    db.flush()
    sg2 = m.Interface(device_id=bng.id, name="smartgroup2", if_type="bundle", speed_gbps=300, oper_status="up")
    sg99 = m.Interface(device_id=bng.id, name="smartgroup99", if_type="bundle", speed_gbps=100, oper_status="up")
    trunk = m.Interface(device_id=me.id, name="Eth-Trunk2", if_type="bundle", speed_gbps=300, oper_status="up")
    db.add_all([sg2, sg99, trunk])
    db.flush()
    pairs = [("cgei-0/0/0/1", sg2, "100GE0/2/0"), ("cgei-0/0/0/2", sg2, "100GE0/1/0")]
    if stray:
        pairs.append(("cgei-0/2/1/8", sg99, "100GE0/1/1"))
    for local, bundle, remote in pairs[:me_members]:
        db.add_all([m.Interface(device_id=bng.id, name=local, speed_gbps=100, parent_id=bundle.id, oper_status="up"),
                    m.Interface(device_id=me.id, name=remote, speed_gbps=100, parent_id=trunk.id, oper_status="up"),
                    m.LldpNeighbor(device_id=bng.id, local_interface=local, remote_system_name=me.hostname,
                                   remote_port_id=remote)])
    db.commit()


def _links(db) -> list[str]:
    return sorted(l.name for l in db.scalars(select(m.Link)))


def test_should_keep_only_the_bundle_most_members_agree_on_when_lldp_points_to_two(db):
    _bng_and_me(db)
    discover_links(db)
    assert _links(db) == ["BDG-BBU-BNG2-M6K-8SP:smartgroup2 <> BDG-BBU-ME1-M8:Eth-Trunk2"]


def test_should_keep_both_bundles_when_their_members_are_evenly_split(db):
    _bng_and_me(db, me_members=2)  # cgei-0/0/0/1 in smartgroup2 ...
    sg99 = db.scalar(select(m.Interface).where(m.Interface.name == "smartgroup99"))
    db.scalar(select(m.Interface).where(m.Interface.name == "cgei-0/0/0/2")).parent_id = sg99.id  # ... one in sg99
    db.commit()
    discover_links(db)
    assert len(_links(db)) == 2
