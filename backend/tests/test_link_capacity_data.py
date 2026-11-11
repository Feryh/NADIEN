"""Link capacity and traffic from imperfect NMS data: bundles with members LLDP only partly sees, saturated
32-bit ifSpeed, and links no longer seen that share a port with their replacement."""

from fastapi.testclient import TestClient
from sqlalchemy import select

from app import models as m
from app.collectors.observium.parsers import parse_port
from app.collectors.observium.sync import SyncReport, _write_utilization
from app.database import get_db
from app.main import app
from app.services.topology import discover_links


def _devices(db) -> tuple[m.Device, m.Device]:
    pop = m.Pop(code="BGR", name="Bogor")
    db.add(pop)
    db.flush()
    a = m.Device(hostname="BGR-SMT-SW1-NX93", pop_id=pop.id, role="access")
    b = m.Device(hostname="PEIN1-H-BGR-MGW", pop_id=pop.id, role="pe")
    db.add_all([a, b])
    db.flush()
    return a, b


def test_should_use_bundle_speed_when_lldp_sees_only_one_member_of_a_bundle(db):
    a, b = _devices(db)
    pc30 = m.Interface(device_id=a.id, name="port-channel30", if_type="bundle", speed_gbps=40, oper_status="up")
    trunk40 = m.Interface(device_id=b.id, name="Eth-Trunk40", if_type="bundle", speed_gbps=80, oper_status="up")
    db.add_all([pc30, trunk40])
    db.flush()
    db.add_all([m.Interface(device_id=a.id, name="Ethernet1/13", speed_gbps=10, parent_id=pc30.id, oper_status="up"),
                m.Interface(device_id=b.id, name="25GE0/1/29", speed_gbps=10, parent_id=trunk40.id, oper_status="up"),
                m.LldpNeighbor(device_id=a.id, local_interface="Ethernet1/13", remote_system_name=b.hostname,
                               remote_port_id="25GE0/1/29")])
    db.commit()
    discover_links(db)
    link = db.scalars(select(m.Link)).one()
    assert (link.link_type, link.capacity_gbps) == ("bundle", 40)  # not the one known 10G member


def test_should_leave_speed_unknown_when_32_bit_ifspeed_is_saturated():
    rec = parse_port({"port_id": "1", "device_id": "1", "ifName": "ethernet0/8/0", "ifSpeed": "4294967295",
                      "ifHighSpeed": "0", "ifAdminStatus": "up", "ifOperStatus": "up"})
    assert rec.speed_gbps is None


def test_should_give_no_traffic_to_a_down_link_when_it_shares_a_port_with_the_live_one(db):
    a, b = _devices(db)
    old, new, far = (m.Interface(device_id=a.id, name="Huawei-MA5800-V100R022-Et 0/8/0"),
                     m.Interface(device_id=a.id, name="ethernet0/8/0"), m.Interface(device_id=b.id, name="XGigabitEthernet0/0/1"))
    db.add_all([old, new, far])
    db.flush()
    stale = m.Link(name="stale", a_device_id=a.id, a_interface_id=old.id, b_device_id=b.id, b_interface_id=far.id,
                   capacity_gbps=4.295, current_gbps=6.3, status="down")
    live = m.Link(name="live", a_device_id=a.id, a_interface_id=new.id, b_device_id=b.id, b_interface_id=far.id,
                  capacity_gbps=10, status="up")
    db.add_all([stale, live])
    db.commit()
    _write_utilization(db, {far.id: (6.3, 7.8)}, SyncReport(parts=["utilization"]))
    assert (stale.current_gbps, live.current_gbps) == (0.0, 7.8)


def test_should_take_traffic_from_the_narrower_bundle_when_its_far_end_bundle_is_wider(db):
    a, b = _devices(db)
    sg210 = m.Interface(device_id=a.id, name="smartgroup210", if_type="bundle", speed_gbps=200)  # one member elsewhere
    sg1 = m.Interface(device_id=b.id, name="smartgroup1", if_type="bundle", speed_gbps=100)
    db.add_all([sg210, sg1])
    db.flush()
    link = m.Link(name="bundle", a_device_id=a.id, a_interface_id=sg210.id, b_device_id=b.id, b_interface_id=sg1.id,
                  capacity_gbps=100, status="up", link_type="bundle")
    db.add(link)
    db.commit()
    _write_utilization(db, {sg210.id: (60.0, 148.7), sg1.id: (55.0, 40.0)}, SyncReport(parts=["utilization"]))
    assert link.current_gbps == 55.0  # smartgroup1's own traffic, not the whole smartgroup210


def test_should_record_one_link_when_lldp_names_a_fiberhome_port_slot_colon_port_from_the_far_end(db):
    pop = m.Pop(code="BDG", name="Bandung")
    db.add(pop)
    db.flush()
    olt = m.Device(hostname="BDG-LMB-OLT1-FH", pop_id=pop.id, role="olt")
    me = m.Device(hostname="BDG-CMH-ME1-6120HS", pop_id=pop.id, role="agg")
    db.add_all([olt, me])
    db.flush()
    db.add_all([m.Interface(device_id=olt.id, name="SFP+ 9/1", speed_gbps=10, oper_status="up"),
                m.Interface(device_id=me.id, name="xgei-1/1/0/6", speed_gbps=10, oper_status="up"),
                # Both LLDP directions: the OLT names its port SFP+ 9/1, the ME1 hears it as 9:1.
                m.LldpNeighbor(device_id=olt.id, local_interface="SFP+ 9/1", remote_system_name=me.hostname,
                               remote_port_id="xgei-1/1/0/6"),
                m.LldpNeighbor(device_id=me.id, local_interface="xgei-1/1/0/6", remote_system_name=olt.hostname,
                               remote_port_id="9:1")])
    db.commit()
    discover_links(db)
    links = db.scalars(select(m.Link)).all()
    assert [(l.name, l.capacity_gbps) for l in links] == [("BDG-LMB-OLT1-FH:SFP+ 9/1 <> BDG-CMH-ME1-6120HS:xgei-1/1/0/6", 10)]


def test_should_leave_down_links_out_when_listing_hot_links(db):
    a, b = _devices(db)
    db.add(m.Link(name="stale", a_device_id=a.id, b_device_id=b.id, capacity_gbps=4.295, current_gbps=6.3, status="down"))
    db.commit()
    app.dependency_overrides[get_db] = lambda: db
    try:
        rows = TestClient(app).get("/api/capacity/hotspots").json()["links"]["rows"]
    finally:
        app.dependency_overrides.clear()
    assert rows == []


def test_should_read_rrd_history_from_the_narrower_bundle_when_its_far_end_bundle_is_wider(db):
    from app.collectors.observium.rrd import _targets

    a, b = _devices(db)
    sg1 = m.Interface(device_id=a.id, name="smartgroup1", if_type="bundle", speed_gbps=200)  # to two switches
    pc47 = m.Interface(device_id=b.id, name="port-channel47", if_type="bundle", speed_gbps=100)
    db.add_all([sg1, pc47])
    db.flush()
    db.add_all([m.Link(name="mlag", a_device_id=a.id, a_interface_id=sg1.id, b_device_id=b.id, b_interface_id=pc47.id,
                       capacity_gbps=100, status="up", link_type="bundle"),
                m.ObserviumPort(interface_id=sg1.id, observium_port_id=11, if_index=1, observium_hostname="a"),
                m.ObserviumPort(interface_id=pc47.id, observium_port_id=22, if_index=2, observium_hostname="b")])
    db.commit()

    class Source:
        def usable(self, ref):
            return True

    (target,), _ = _targets(db, Source())
    assert (target.ref.observium_port_id, target.swapped) == (22, True)


def test_should_leave_rate_unknown_when_observium_reports_more_than_the_port_can_carry():
    rec = parse_port({"port_id": "1", "device_id": "1", "ifName": "ethernet0/9/0", "ifHighSpeed": "10000",
                      "ifOperStatus": "up", "ifInOctets_rate": str(int(548832e9 / 8)), "ifOutOctets_rate": "125000000"})
    assert (rec.in_gbps, rec.out_gbps) == (None, 1.0)


def test_should_show_the_narrower_ends_live_rate_next_to_the_planning_value_in_hotspots(db):
    from app.routers.capacity import _hotspots

    a, b = _devices(db)
    sg1 = m.Interface(device_id=a.id, name="smartgroup1", if_type="bundle", speed_gbps=200, in_gbps=150.0, out_gbps=9.0)
    pc47 = m.Interface(device_id=b.id, name="port-channel47", if_type="bundle", speed_gbps=100, in_gbps=8.0, out_gbps=81.5)
    db.add_all([sg1, pc47])
    db.flush()
    db.add(m.Link(name="mlag", a_device_id=a.id, a_interface_id=sg1.id, b_device_id=b.id, b_interface_id=pc47.id,
                  capacity_gbps=100, current_gbps=84.0, status="up", link_type="bundle"))
    db.commit()
    (row,) = _hotspots(db, 10)["links"]["rows"]
    assert (row["current_gbps"], row["live_gbps"]) == (84.0, 81.5)


def test_should_read_the_wider_end_when_the_narrower_one_counts_no_traffic(db):
    from app.collectors.observium.rrd import _targets

    a, b = _devices(db)
    trunk = m.Interface(device_id=a.id, name="Eth-Trunk5", if_type="bundle", speed_gbps=200, in_gbps=80.0, out_gbps=6.0)
    vlanif = m.Interface(device_id=b.id, name="Vbdif59", if_type="virtual", speed_gbps=1, in_gbps=0.0, out_gbps=0.0)
    db.add_all([trunk, vlanif])
    db.flush()
    db.add_all([m.Link(name="vlanif", a_device_id=a.id, a_interface_id=trunk.id, b_device_id=b.id, b_interface_id=vlanif.id,
                       capacity_gbps=1, status="up"),
                m.ObserviumPort(interface_id=trunk.id, observium_port_id=11, if_index=1, observium_hostname="a"),
                m.ObserviumPort(interface_id=vlanif.id, observium_port_id=22, if_index=2, observium_hostname="b")])
    db.commit()

    class Source:
        def usable(self, ref):
            return True

    (target,), _ = _targets(db, Source())
    assert (target.ref.observium_port_id, target.swapped) == (11, False)
