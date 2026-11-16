"""Far end of an Observium LLDP neighbour: the port the neighbour advertised beats Observium's own guess."""

from sqlalchemy import select

from app import models as m
from app.collectors.observium.parsers import advertised_port
from app.collectors.observium.sync import sync_observium

from .test_observium_csr import _client, _port

DEVICES = {"1": {"device_id": "1", "sysName": "OLT1-C300-JKTT-PGD", "hostname": "OLT1-C300-JKTT-PGD", "os": "zxa10"},
           "2": {"device_id": "2", "sysName": "PE1-ATN910-JKT-PGD", "hostname": "PE1-ATN910-JKT-PGD", "os": "vrp"}}
PORTS = {"10": _port("10", "1", "xgei_1/19/1"),
         "20": _port("20", "2", "Ethernet0/0/0"),  # the 10M console port Observium picks
         "21": _port("21", "2", "GigabitEthernet0/2/0")}


def _neighbour(remote_port: str, remote_port_id: str) -> dict:
    return {"1": {"neighbour_id": "1", "device_id": "1", "port_id": "10", "protocol": "lldp", "active": "1",
                  "remote_hostname": "PE1-ATN910-JKT-PGD", "remote_port": remote_port, "remote_port_id": remote_port_id}}


def _far_port(db) -> str:
    return db.scalar(select(m.LldpNeighbor.remote_port_id))


def test_should_drop_the_description_note_from_an_advertised_port():
    assert advertised_port("GigabitEthernet0/2/0(xgei_1/19/1)") == "GigabitEthernet0/2/0"


def test_should_keep_an_advertised_port_that_is_only_a_note():
    assert advertised_port("(unknown)") == "(unknown)"


def test_should_use_the_advertised_port_when_observium_resolves_the_far_end_to_another_port(db):
    neighbours = _neighbour("GigabitEthernet0/2/0(xgei_1/19/1)", "20")
    sync_observium(db, _client(DEVICES, PORTS, neighbours), {"inventory", "neighbours"}, False)
    assert _far_port(db) == "GigabitEthernet0/2/0"


def test_should_trust_observium_when_the_advertised_port_is_no_port_of_the_far_device(db):
    sync_observium(db, _client(DEVICES, PORTS, _neighbour("Gi0/2/0", "21")), {"inventory", "neighbours"}, False)
    assert _far_port(db) == "GigabitEthernet0/2/0"
