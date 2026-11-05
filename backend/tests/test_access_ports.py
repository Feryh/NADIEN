"""Access-port choices for an L2VPN: UP ports towards OLT / access, bundles first, uplinks left out."""

import pytest
from fastapi.testclient import TestClient

from app import models as m
from app.database import get_db
from app.main import app
from app.services.access_ports import access_ports


@pytest.fixture
def pe(db) -> int:
    pop = m.Pop(code="JKT", name="Jakarta")
    db.add(pop)
    db.flush()
    pe = m.Device(hostname="JKT-PE1", pop_id=pop.id, role="pe")
    core = m.Device(hostname="JKT-P1", pop_id=pop.id, role="p")
    olt = m.Device(hostname="JKT-OLT1", pop_id=pop.id, role="olt")
    olt2 = m.Device(hostname="JKT-OLT2", pop_id=pop.id, role="olt")
    db.add_all([pe, core, olt, olt2])
    db.flush()

    def port(name: str, if_type: str = "physical", status: str = "up", parent: m.Interface | None = None,
             description: str | None = None) -> m.Interface:
        iface = m.Interface(device_id=pe.id, name=name, if_type=if_type, oper_status=status,
                            parent_id=parent.id if parent else None, description=description)
        db.add(iface)
        db.flush()
        return iface

    trunk1 = port("Eth-Trunk1", "bundle")
    port("GE0/1/0", parent=trunk1)
    port("GE0/1/1", parent=trunk1)
    trunk2 = port("Eth-Trunk2", "bundle")
    port("GE0/6/0", parent=trunk2)
    port("GE0/6/1", parent=trunk2)
    uplink = port("GE0/2/0")
    port("GE0/3/0", description="TO-OLT-SITE-X")
    port("GE0/4/0", status="down")
    port("GE0/5/0")
    port("LoopBack0")
    for local, remote in (("GE0/1/0", "JKT-OLT1"), ("GE0/1/1", "JKT-OLT1"), ("GE0/6/0", "JKT-OLT1"),
                          ("GE0/6/1", "JKT-OLT2")):
        db.add(m.LldpNeighbor(device_id=pe.id, local_interface=local, remote_system_name=remote, remote_port_id="x"))
    db.add(m.Link(name="pe-p", a_device_id=pe.id, a_interface_id=uplink.id, b_device_id=core.id, capacity_gbps=10))
    db.commit()
    return pe.id


def test_should_offer_bundle_first_when_all_members_reach_the_same_olt(db, pe):
    first = access_ports(db, pe)["ports"][0]
    assert (first["name"], first["direction"], first["far"]["hostname"]) == ("Eth-Trunk1", "downlink", "JKT-OLT1")
    assert first["members"] == ["GE0/1/0", "GE0/1/1"]


def test_should_never_offer_a_bundle_member_on_its_own(db, pe):
    names = {p["name"] for p in access_ports(db, pe)["ports"]}
    assert not names & {"GE0/1/0", "GE0/1/1", "GE0/6/0", "GE0/6/1"}


def test_should_leave_out_uplinks_down_ports_and_loopbacks(db, pe):
    result = access_ports(db, pe)
    names = {p["name"] for p in result["ports"]}
    assert not names & {"GE0/2/0", "GE0/4/0", "LoopBack0"} and result["hidden_uplinks"] == 1


def test_should_read_downlink_from_port_description_when_no_neighbour_is_known(db, pe):
    port = next(p for p in access_ports(db, pe)["ports"] if p["name"] == "GE0/3/0")
    assert (port["direction"], port["basis"]) == ("downlink", "description")


def test_should_keep_split_bundle_direction_unknown_when_members_reach_different_devices(db, pe):
    port = next(p for p in access_ports(db, pe)["ports"] if p["name"] == "Eth-Trunk2")
    assert port["direction"] == "unknown" and port["far"] is None


def test_should_serve_port_choices_over_the_api(db, pe):
    app.dependency_overrides[get_db] = lambda: db
    try:
        client = TestClient(app)
        assert client.get("/api/configs/l2vpn/ports", params={"device_id": pe}).json()["ports"][0]["name"] == "Eth-Trunk1"
        assert client.get("/api/configs/l2vpn/ports", params={"device_id": 999999}).status_code == 404
    finally:
        app.dependency_overrides.clear()
