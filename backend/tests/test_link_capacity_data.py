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
