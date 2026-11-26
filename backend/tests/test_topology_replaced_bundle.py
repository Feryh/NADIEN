"""A bundle still described toward a replaced box while LLDP sees its members on the new one (PE1-ATN910-NTT-BAJAWA
Eth-Trunk2: members to PE1-NCS57C3-NTT-ENDE, description to the old PE1-ATN910-NTT-ENDE)."""

from sqlalchemy import select

from app import models as m
from app.services.topology import discover_links

MEMBERS = ("GigabitEthernet0/2/2", "GigabitEthernet0/2/3")


def _network(db, lldp: bool = True) -> None:
    pop = m.Pop(code="NTT", name="Nusa Tenggara Timur")
    db.add(pop)
    db.flush()
    bajawa, old_ende, new_ende = (m.Device(hostname=h, pop_id=pop.id) for h in (
        "PE1-ATN910-NTT-BAJAWA", "PE1-ATN910-NTT-ENDE", "PE1-NCS57C3-NTT-ENDE"))
    db.add_all([bajawa, old_ende, new_ende])
    db.flush()
    trunk = m.Interface(device_id=bajawa.id, name="Eth-Trunk2", if_type="bundle", speed_gbps=40, oper_status="up",
                        description="INTERNAL:[BB]TO-PE1-ATN910-NTT-ENDE-ETH-TRUNK2 [40G]")
    old_trunk = m.Interface(device_id=old_ende.id, name="Eth-Trunk2", if_type="bundle", speed_gbps=4, oper_status="up",
                            description="INTERNAL:[BB]TO-PE1-ATN910-NTT-BAJAWA-Eth-trunk2 [40G]")
    db.add_all([trunk, old_trunk])
    db.flush()
    for k, name in enumerate(MEMBERS):
        db.add(m.Interface(device_id=bajawa.id, name=name, if_type="physical", speed_gbps=10, oper_status="up",
                           parent_id=trunk.id))
        db.add(m.Interface(device_id=new_ende.id, name=f"TenGigE0/0/0/{k}", if_type="physical", speed_gbps=10,
                           oper_status="up"))
        if lldp:
            db.add(m.LldpNeighbor(device_id=bajawa.id, local_interface=name, remote_system_name="PE1-NCS57C3-NTT-ENDE",
                                  remote_port_id=f"TenGigE0/0/0/{k}"))
    db.commit()


def _links(db) -> list[str]:
    return sorted(l.name for l in db.scalars(select(m.Link)))


def test_should_pair_the_bundles_by_description_when_lldp_says_nothing_about_them(db):
    _network(db, lldp=False)
    discover_links(db)
    assert _links(db) == ["PE1-ATN910-NTT-BAJAWA:Eth-Trunk2 <> PE1-ATN910-NTT-ENDE:Eth-Trunk2"]


def test_should_not_pair_a_bundle_by_description_when_lldp_links_its_members_to_another_box(db):
    _network(db)
    discover_links(db)
    assert all("PE1-ATN910-NTT-ENDE" not in name for name in _links(db)) and len(_links(db)) == 2


def test_should_delete_the_old_description_link_when_lldp_now_links_the_members_to_the_replacement(db):
    _network(db, lldp=False)
    discover_links(db)
    for k, name in enumerate(MEMBERS):
        bajawa = db.scalar(select(m.Device.id).where(m.Device.hostname == "PE1-ATN910-NTT-BAJAWA"))
        db.add(m.LldpNeighbor(device_id=bajawa, local_interface=name, remote_system_name="PE1-NCS57C3-NTT-ENDE",
                              remote_port_id=f"TenGigE0/0/0/{k}"))
    db.commit()
    result = discover_links(db)
    assert result.superseded == 1 and all("PE1-ATN910-NTT-ENDE:" not in name for name in _links(db))
