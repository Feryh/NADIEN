"""Device topology with area / province / regency / POP as filters: devices and links only, no group boxes."""

import pytest
from fastapi.testclient import TestClient

from app import models as m
from app.database import get_db
from app.main import app
from app.services import topology_devices


@pytest.fixture
def client(db):
    app.dependency_overrides[get_db] = lambda: db
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


@pytest.fixture
def network(db):
    """Two Jakarta devices linked to each other, one of them also to a Bandung device."""
    pops = {code: m.Pop(code=code, name=code) for code in ("JKT", "BDG")}
    db.add_all(pops.values())
    db.flush()
    pe = m.Device(hostname="JKT-NDC-PE1", pop_id=pops["JKT"].id, role="pe")
    olt = m.Device(hostname="JKT-NDC-OLT1-ZTE", pop_id=pops["JKT"].id, role="olt")
    bdg = m.Device(hostname="BDG-NDC-PE1", pop_id=pops["BDG"].id, role="pe")
    db.add_all([pe, olt, bdg])
    db.flush()
    db.add_all([m.Link(name="pe-olt", a_device_id=pe.id, b_device_id=olt.id, capacity_gbps=10),
                m.Link(name="jkt-bdg", a_device_id=pe.id, b_device_id=bdg.id, capacity_gbps=100)])
    db.commit()


def test_should_draw_only_devices_when_a_pop_is_chosen_as_filter(client, network):
    view = client.get("/api/topology/view", params={"level": "devices", "pop": "JKT"}).json()
    assert {n["kind"] for n in view["nodes"]} == {"device"}
    assert sorted(n["label"] for n in view["nodes"] if not n.get("outside")) == ["JKT-NDC-OLT1-ZTE", "JKT-NDC-PE1"]


def test_should_end_a_link_leaving_the_filter_on_the_far_device_itself(client, network):
    view = client.get("/api/topology/view", params={"level": "devices", "pop": "JKT"}).json()
    (far,) = [n for n in view["nodes"] if n.get("outside")]
    assert far["label"] == "BDG-NDC-PE1" and any(far["id"] in (e["source"], e["target"]) for e in view["edges"])


def test_should_list_every_pop_with_devices_as_filter_choices(client, network):
    rows = client.get("/api/topology/scopes").json()
    assert sorted((r["pop"], r["devices"]) for r in rows) == [("BDG", 1), ("JKT", 2)]


def test_should_ask_to_narrow_the_filters_when_too_many_devices_match(client, network, monkeypatch):
    monkeypatch.setattr(topology_devices, "MAX_DEVICES", 2)
    response = client.get("/api/topology/view", params={"level": "devices"})
    assert response.status_code == 422 and "persempit" in response.json()["detail"]
