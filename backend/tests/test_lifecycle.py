"""Devices gone from the network: labelled for 7 days, then their links and LLDP rows go and they are hidden."""

from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app import models as m
from app.services.topology import discover_links

NOW = datetime.now(timezone.utc)


def _pair(db, polled_ago_days: float | None = 0.0, status: str = "active", neighbour_sees_it: bool = False,
          bng_port_up: bool | None = None):
    """SRG-BNG-HW-M4 (polled now) and a neighbour box, linked by LLDP both ways. Both LLDP rows date from the box's
    last poll unless the BNG still sees it; the BNG's port to the box is down once the box is gone."""
    if bng_port_up is None:
        bng_port_up = neighbour_sees_it or not polled_ago_days
    pop = m.Pop(code="SRG", name="Serang")
    db.add(pop)
    db.flush()
    bng = m.Device(hostname="SRG-BNG-HW-M4", pop_id=pop.id, last_polled_at=NOW)
    box = m.Device(hostname="SRG-SMY-ME1-FH820", pop_id=pop.id, status=status,
                   last_polled_at=None if polled_ago_days is None else NOW - timedelta(days=polled_ago_days))
    db.add_all([bng, box])
    db.flush()
    db.add_all([m.Interface(device_id=bng.id, name="100GE0/5/1", speed_gbps=100,
                            oper_status="up" if bng_port_up else "down", oper_changed_at=NOW - timedelta(days=1)),
                m.Interface(device_id=box.id, name="eth-100gi0/1/0/25", speed_gbps=100, oper_status="up")])
    seen = NOW - timedelta(days=polled_ago_days or 0)
    db.add_all([
        m.LldpNeighbor(device_id=bng.id, local_interface="100GE0/5/1", remote_system_name="SRG-SMY-ME1-FH820",
                       remote_port_id="eth-100gi0/1/0/25", collected_at=NOW if neighbour_sees_it else seen),
        # the BNG's other neighbours, polled now: the newest LLDP rows are from today
        m.LldpNeighbor(device_id=bng.id, local_interface="100GE0/5/2", remote_system_name="SRG-SRG-ME1-FH820",
                       remote_port_id="eth-100gi0/1/0/26", collected_at=NOW),
        m.LldpNeighbor(device_id=box.id, local_interface="eth-100gi0/1/0/25", remote_system_name="SRG-BNG-HW-M4",
                       remote_port_id="100GE0/5/1", collected_at=seen),
    ])
    db.commit()
    return box


def test_should_label_and_draw_down_a_device_observium_stopped_polling_days_ago(db):
    box = _pair(db, polled_ago_days=3)
    result = discover_links(db)
    (link,) = db.scalars(select(m.Link)).all()
    assert result.devices_gone == 1 and not box.hidden and link.status == "down"
    assert box.gone_since.replace(tzinfo=None) == box.last_polled_at.replace(tzinfo=None)


def test_should_not_count_an_unpolled_device_as_gone_while_its_neighbours_still_see_it(db):
    box = _pair(db, polled_ago_days=16, neighbour_sees_it=True)  # TNG-COK-ME1-6120HS: unpolled, its OLTs see it
    discover_links(db)
    assert (box.gone_since, box.hidden) == (None, False)
    assert [l.status for l in db.scalars(select(m.Link))] == ["up"]


def test_should_not_count_an_unpolled_device_as_gone_while_the_polled_end_of_its_link_is_up(db):
    box = _pair(db, polled_ago_days=3, bng_port_up=True)  # OLT1-C300-JKTT-KRJ: PEIN1's trunk to it carries traffic
    discover_links(db)  # draws the link while the box is still alive
    box.last_polled_at -= timedelta(days=10)
    db.commit()
    discover_links(db)
    assert (box.gone_since, box.hidden) == (None, False) and len(db.scalars(select(m.Link)).all()) == 1


def test_should_count_a_swapped_device_as_gone_though_its_neighbours_still_name_it(db):
    """TNG-COK-ME1-6120HS, swapped for TNG-COK-ME1-M8: its neighbours' LLDP still names it, their ports are up."""
    box = _pair(db, polled_ago_days=16, neighbour_sees_it=True)
    db.add(m.Device(hostname="SRG-SMY-ME1-M8", pop_id=box.pop_id, last_polled_at=NOW))
    db.commit()
    discover_links(db)
    assert box.hidden and db.scalars(select(m.Link)).all() == []


def test_should_not_take_another_sites_box_for_a_replacement_when_the_name_ends_in_a_site_code(db):
    """OLT1-C300-JKTT-KRJ is unpolled but its PEIN1 trunk carries traffic; OLT1-C300-JKTT-BKS is another site."""
    pop = m.Pop(code="JKT", name="Jakarta")
    db.add(pop)
    db.flush()
    pe = m.Device(hostname="PEIN1-H-JKTT-KRJ", pop_id=pop.id, last_polled_at=NOW)
    olt = m.Device(hostname="OLT1-C300-JKTT-KRJ", pop_id=pop.id, last_polled_at=NOW - timedelta(days=14))
    other = m.Device(hostname="OLT1-C300-JKTT-BKS", pop_id=pop.id, last_polled_at=NOW)
    db.add_all([pe, olt, other])
    db.flush()
    pe_port = m.Interface(device_id=pe.id, name="Eth-Trunk11", oper_status="up", oper_changed_at=NOW - timedelta(days=7))
    olt_port = m.Interface(device_id=olt.id, name="xgei_1/20/1", oper_status="up")
    db.add_all([pe_port, olt_port])
    db.flush()
    db.add(m.Link(name="krj", a_device_id=olt.id, a_interface_id=olt_port.id, b_device_id=pe.id,
                  b_interface_id=pe_port.id, discovery_source="manual"))
    db.commit()
    discover_links(db)
    assert (olt.gone_since, olt.hidden) == (None, False)


def test_should_delete_links_and_lldp_and_hide_a_device_not_polled_for_over_a_week(db):
    box = _pair(db, polled_ago_days=3)
    discover_links(db)
    box.gone_since -= timedelta(days=6)
    db.commit()
    result = discover_links(db)
    assert result.devices_hidden == 1 and result.gone_links_deleted == 1 and box.hidden
    assert db.scalars(select(m.Link)).all() == []
    assert db.scalars(select(m.LldpNeighbor).where(m.LldpNeighbor.device_id == box.id)).all() == []
    assert db.get(m.Device, box.id) is not None  # the record stays


def test_should_bring_back_a_hidden_device_once_it_is_polled_again(db):
    box = _pair(db, polled_ago_days=10)
    discover_links(db)
    box.last_polled_at = NOW
    db.commit()
    result = discover_links(db)
    assert result.devices_back == 1 and (box.gone_since, box.hidden) == (None, False)


def test_should_hide_a_device_decommissioned_more_than_a_week_ago(db):
    box = _pair(db, status="decommissioned")
    discover_links(db)
    assert box.gone_since is not None and not box.hidden  # just decommissioned: labelled first
    box.gone_since -= timedelta(days=8)
    db.commit()
    discover_links(db)
    assert box.hidden and db.scalars(select(m.Link)).all() == []


def test_should_leave_devices_observium_never_polled_alone(db):
    box = _pair(db, polled_ago_days=None)  # e.g. a cell-site router known from RANCID configs only
    result = discover_links(db)
    assert result.devices_gone == 0 and box.gone_since is None
    assert [l.status for l in db.scalars(select(m.Link))] == ["up"]


def test_should_delete_lldp_rows_older_than_the_age_limit(db):
    _pair(db)
    bng = db.scalar(select(m.Device).where(m.Device.hostname == "SRG-BNG-HW-M4"))
    db.add(m.LldpNeighbor(device_id=bng.id, local_interface="100GE0/5/9", remote_system_name="OLD-BOX",
                          remote_port_id="ge-0/0/1", collected_at=NOW - timedelta(days=20)))
    db.commit()
    assert discover_links(db).lldp_rows_deleted == 1


def test_should_keep_a_twin_hidden_when_it_was_decommissioned_without_ever_being_polled(db):
    pop = m.Pop(code="JKT", name="Jakarta")
    db.add(pop)
    db.flush()
    twin = m.Device(hostname='"JKT-CSF-CLDMGMT-01"', pop_id=pop.id, status="decommissioned", hidden=True,
                    gone_since=datetime(2000, 1, 1, tzinfo=timezone.utc))
    db.add(twin)
    db.commit()
    discover_links(db)
    assert twin.hidden is True
