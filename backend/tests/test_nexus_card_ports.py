"""Nexus switch ports Observium leaves out (no transceiver, never used): the RANCID config still lists every port
of the module, and its `!Slot` line says how fast each group of ports is."""

from sqlalchemy import select

from app import models as m
from app.collectors.configs.apply import apply_config
from app.collectors.configs.ios import parse
from app.routers.analysis import _device_port_usage

PORTS = "\n".join(f"interface Ethernet1/{n}\n" for n in range(1, 55) if n != 7)
CONFIG = f"""!RANCID-CONTENT-TYPE: cisco
!Image:         NXOS Image: bootflash:///nxos64-cs.10.4.5.M.bin
!Slot 1: type 48x10/25G/32G + 6x40/100G Ethernet/FC Module, 54 ports
!Slot 1: part N9K-C93180YC-FX, serial ok
!
!Slot 27: type Virtual Supervisor Module                        N9K-C93180YC-FX, 0 ports
hostname JMB-NDC-SW1-NX93

interface port-channel8
  description TO_JBI-NDC-F1A-NE8K_2x10G_Eth.TR8

interface Ethernet1/7
  description JBI-NDC-BNG-HW-F1A:GigabitEthernet 0/1/1
  channel-group 8 mode active
  no shutdown

{PORTS}
interface Ethernet1/3
  description new OLT JMB-TLP-OLT1-ZTE
  no shutdown

interface mgmt0
  ip address 192.168.10.2/24
"""


def _nexus(db) -> m.Device:
    pop = m.Pop(code="JMB", name="Jambi")
    db.add(pop)
    db.flush()
    switch = m.Device(hostname="JMB-NDC-SW1-NX93", pop_id=pop.id, role="access")
    db.add(switch)
    db.flush()
    port = m.Interface(device_id=switch.id, name="Ethernet1/7", if_type="physical", mib_type="ethernetCsmacd",
                       speed_gbps=10.0, admin_status="up", oper_status="up")
    db.add(port)
    db.flush()
    db.add(m.ObserviumPort(interface_id=port.id, observium_port_id=port.id, observium_hostname=switch.hostname))
    db.commit()
    apply_config(db, switch, parse(CONFIG), create_ports=False)
    return switch


def test_should_give_each_nexus_port_the_speed_of_its_module_group():
    ports = {p.name: p.speed_gbps for p in parse(CONFIG).interfaces.values() if p.card_port}
    assert len(ports) == 54 and ports["Ethernet1/48"] == 25.0 and ports["Ethernet1/49"] == 100.0


def test_should_treat_a_nexus_port_without_no_shutdown_as_shut_down():
    ports = parse(CONFIG).interfaces
    assert ports["Ethernet1/8"].shutdown and not ports["Ethernet1/3"].shutdown


def test_should_add_the_nexus_ports_observium_leaves_out(db):
    switch = _nexus(db)
    names = set(db.scalars(select(m.Interface.name).where(m.Interface.device_id == switch.id,
                                                          m.Interface.if_type == "physical")))
    assert len(names) == 54


def test_should_count_48_25g_and_6_100g_ports_when_observium_reports_one_port(db):
    switch = _nexus(db)
    (row,) = [r for r in _device_port_usage(db, None, None, None, None, None) if r["device_id"] == switch.id]
    assert {speed: counts["used"] + counts["available"] for speed, counts in row["speeds"].items()} == \
        {"10G": 1, "25G": 47, "100G": 6}


def test_should_not_mark_catalyst_ports_as_card_ports_when_there_is_no_slot_line():
    cfg = parse("interface GigabitEthernet1/0/1\n description x\n")
    assert not any(p.card_port for p in cfg.interfaces.values())
