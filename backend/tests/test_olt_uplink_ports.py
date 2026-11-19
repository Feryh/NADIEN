"""OLT uplink ports Observium leaves out (Huawei MPU boards) and empty SFP+ cages it reports at 1G (FiberHome)."""

from sqlalchemy import select

from app import models as m
from app.collectors.configs.apply import apply_config
from app.collectors.configs.vrp import parse
from app.routers.analysis import _device_port_usage
from app.services.areas import port_bucket

# Layout of a Huawei MA5800 config: section markers, inconsistent indentation, board blocks with per-port lines.
CONFIG = """  <vlan-config>
 port vlan 355 0/8 0
  <mpu-0/8>
interface mpu 0/8
 speed 0 10000
 auto-neg 1 disable
 speed 1 10000
 shutdown 1
  <mpu-0/9>
interface mpu 0/9
 auto-neg 0 disable
 speed 0 10000
 speed 1 10000
 speed 2 10000
 speed 3 10000
 shutdown 1
 shutdown 2
  <gpon-0/1>
 interface gpon 0/1
 port 0 ont-auto-find enable
 shutdown 3
"""


def _olt(db) -> m.Device:
    pop = m.Pop(code="TNG", name="Tangerang")
    db.add(pop)
    db.flush()
    olt = m.Device(hostname="TNG-BSD-OLT5-HW", pop_id=pop.id, role="olt")
    db.add(olt)
    db.flush()
    for name in ("ethernet0/8/0", "ethernet0/8/1", "ethernet0/9/0"):
        port = m.Interface(device_id=olt.id, name=name, if_type="physical", mib_type="ethernetCsmacd",
                           speed_gbps=10.0, admin_status="up", oper_status="up")
        db.add(port)
        db.flush()
        db.add(m.ObserviumPort(interface_id=port.id, observium_port_id=port.id, observium_hostname=olt.hostname))
    db.commit()
    apply_config(db, olt, parse(CONFIG), create_ports=False)
    return olt


def test_should_read_every_port_of_a_huawei_olt_uplink_board():
    ports = {p.name: (p.speed_gbps, p.shutdown) for p in parse(CONFIG).interfaces.values() if p.card_port}
    assert ports == {"ethernet0/8/0": (10.0, False), "ethernet0/8/1": (10.0, True), "ethernet0/9/0": (10.0, False),
                     "ethernet0/9/1": (10.0, True), "ethernet0/9/2": (10.0, True), "ethernet0/9/3": (10.0, False)}


def test_should_add_the_uplink_ports_observium_leaves_out(db):
    olt = _olt(db)
    names = set(db.scalars(select(m.Interface.name).where(m.Interface.device_id == olt.id)))
    assert names == {"ethernet0/8/0", "ethernet0/8/1", "ethernet0/9/0", "ethernet0/9/1", "ethernet0/9/2",
                     "ethernet0/9/3"}


def test_should_count_the_added_uplink_ports_as_available_10g(db):
    olt = _olt(db)
    (row,) = [r for r in _device_port_usage(db, None, None, None, None, None) if r["device_id"] == olt.id]
    assert row["speeds"]["10G"] == {"used": 3, "idle": 1, "disabled": 2, "available": 3}


def test_should_class_an_empty_fiberhome_sfp_plus_cage_as_10g():
    assert port_bucket("SFP+ 8/2", 1.0) == "10G" and port_bucket("XFP 9/1", 1.0) == "10G"
