"""ZTE OLT PON ports from the RANCID config: technology for port capacity, and the ports Observium leaves out."""

from sqlalchemy import select

from app import models as m
from app.collectors.configs.apply import apply_config
from app.collectors.configs.zte import parse
from app.routers.analysis import _device_port_usage
from app.services.areas import port_bucket

CONFIG = """hostname SRG-PLA-OLT1-ZTE
interface gpon_olt-1/3/4
$
interface gpon_olt-1/3/5
$
interface gpon_olt-1/4/16
$
interface xgei-1/10/1
  description UPLINK|xgei-1/10/1|CLG-MSG-ME1-FH820
$
interface gpon_olt-1/3/4
  no shutdown
$
interface gpon_olt-1/3/5
  no shutdown
$
interface gpon_olt-1/4/16
  shutdown
$
"""


def _olt(db) -> m.Device:
    pop = m.Pop(code="SRG", name="Serang")
    db.add(pop)
    db.flush()
    olt = m.Device(hostname="SRG-PLA-OLT1-ZTE", pop_id=pop.id, role="olt")
    db.add(olt)
    db.flush()
    for name, kind, speed in (("Slot3/Port4", "PonStatus", 10.0), ("xgei-1/10/1", "ethernetCsmacd", 10.0)):
        port = m.Interface(device_id=olt.id, name=name, if_type="physical", mib_type=kind, speed_gbps=speed,
                           admin_status="up", oper_status="up")
        db.add(port)
        db.flush()
        db.add(m.ObserviumPort(interface_id=port.id, observium_port_id=port.id, observium_hostname=olt.hostname))
    db.commit()
    apply_config(db, olt, parse(CONFIG), create_ports=False)
    return olt


def test_should_read_the_pon_technology_from_c600_and_c300_interface_names():
    cfg = parse("interface gpon_olt-1/3/5\n$\ninterface gpon-olt_1/2/1\n$\ninterface xgspon_olt-1/7/2\n$\n")
    assert [(p.pon, p.pon_slot_port) for p in cfg.interfaces.values()] == [
        ("GPON", (3, 5)), ("GPON", (2, 1)), ("XGS-PON", (7, 2))]


def test_should_class_a_gpon_port_as_2_5g_whatever_ifspeed_says():
    assert port_bucket("Slot1/Port1", 10.0, "GPON") == "2.5G" and port_bucket("xgei-1/10/1", 10.0) == "10G"


def test_should_add_the_pon_ports_observium_leaves_out_as_idle_and_disabled(db):
    olt = _olt(db)
    ports = {p.name: (p.admin_status, p.oper_status, p.optic_type)
             for p in db.scalars(select(m.Interface).where(m.Interface.device_id == olt.id))}
    assert ports["Slot3/Port5"] == ("up", "down", "GPON") and ports["Slot4/Port16"] == ("down", "down", "GPON")
    assert ports["Slot3/Port4"][2] == "GPON" and "gpon_olt-1/3/5" not in ports


def test_should_count_pon_ports_as_2_5g_and_uplinks_as_10g_in_port_capacity(db):
    olt = _olt(db)
    (row,) = [r for r in _device_port_usage(db, None, None, None, None, None) if r["device_id"] == olt.id]
    assert row["speeds"]["2.5G"] == {"used": 1, "idle": 1, "disabled": 1, "available": 2}
    assert row["speeds"]["10G"]["used"] == 1
