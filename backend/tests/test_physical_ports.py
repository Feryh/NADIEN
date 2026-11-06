"""Port capacity counts physical ports only, at the speed Observium reports (ifHighSpeed / ifSpeed)."""

import pytest
from fastapi.testclient import TestClient

from app import models as m
from app.collectors.observium.parsers import classify_port, parse_port
from app.database import get_db
from app.main import app


@pytest.mark.parametrize("name, if_type, kind", [
    ("GigabitEthernet0/0/1", "ethernetCsmacd", "physical"),
    ("slot 1 pon 2", "PonStatus", "physical"),          # PON ports are physical ports
    ("Vlan100", "propVirtual", "virtual"),
    ("xgei-1/1/0/1", "l2vlan", "virtual"),             # ifType wins over an ordinary-looking name
    ("dcn_eth1", "", "virtual"),                       # no ifType known: the name says management
    ("MEth0/0/1", "", "virtual"),
    ("Eth-Trunk1", "propMultiplexor", "bundle"),
])
def test_should_classify_port_from_observium_iftype_and_name(name, if_type, kind):
    assert classify_port(name, if_type) == kind


def test_should_keep_observium_iftype_and_speed_when_parsing_a_port():
    rec = parse_port({"port_id": 1, "device_id": 1, "ifName": "Vlan20", "ifType": "propVirtual", "ifHighSpeed": 1000})
    assert (rec.if_type, rec.mib_type, rec.speed_gbps) == ("virtual", "propVirtual", 1.0)


@pytest.fixture
def client(db):
    app.dependency_overrides[get_db] = lambda: db
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


def test_should_count_only_observium_physical_ports_in_port_capacity(client, db):
    pop = m.Pop(code="JKT", name="Jakarta")
    db.add(pop)
    db.flush()
    device = m.Device(hostname="JKT-PE1", pop_id=pop.id, role="pe")
    db.add(device)
    db.flush()
    seen = m.Interface(device_id=device.id, name="Hu0/0/0/0", speed_gbps=100, oper_status="up")
    svi = m.Interface(device_id=device.id, name="Vlan100", if_type="virtual", speed_gbps=1, oper_status="up")
    config_only = m.Interface(device_id=device.id, name="Hu0/0/0/9", speed_gbps=100, oper_status="up")
    db.add_all([seen, svi, config_only])
    db.flush()
    db.add_all([m.ObserviumPort(interface_id=i.id, observium_port_id=i.id, observium_hostname="JKT-PE1")
                for i in (seen, svi)])
    db.commit()
    body = client.get("/api/inventory/ports/summary").json()
    assert body["totals"]["total"] == 1 and body["by_speed"][0]["speed"] == "100G"
    assert body["config_only_ports"] == 1
