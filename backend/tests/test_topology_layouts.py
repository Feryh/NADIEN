"""Hand-arranged node positions per topology view, for every node kind (areas, POPs, other groups), not only devices."""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import models as m
from app.database import get_db
from app.main import app


@pytest.fixture
def client(db):
    app.dependency_overrides[get_db] = lambda: db
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


@pytest.fixture
def network(db):
    pops = {code: m.Pop(code=code, name=code) for code in ("JKT", "BDG")}
    db.add_all(pops.values())
    db.flush()
    jkt = m.Device(hostname="JKT-NDC-PE1", pop_id=pops["JKT"].id, role="pe", map_x=10, map_y=20)
    bdg = m.Device(hostname="BDG-NDC-PE1", pop_id=pops["BDG"].id, role="pe")
    db.add_all([jkt, bdg])
    db.flush()
    db.add(m.Link(name="jkt-bdg", a_device_id=jkt.id, b_device_id=bdg.id, capacity_gbps=10))
    db.commit()


def test_should_return_saved_positions_of_area_nodes_when_reopening_the_area_view(client, network):
    view = client.get("/api/topology/view", params={"level": "area"}).json()
    assert view["layout_key"] == "area" and view["layout"] == {}
    response = client.put("/api/topology/layouts", json={"view_key": "area",
                                                          "positions": {"area:Jabodetabek": {"x": 120, "y": -40}}})
    assert response.status_code == 204
    view = client.get("/api/topology/view", params={"level": "area"}).json()
    assert view["layout"] == {"area:Jabodetabek": {"x": 120.0, "y": -40.0}}


def test_should_keep_positions_of_other_pops_on_the_device_view(client, network):
    client.put("/api/topology/layouts", json={"view_key": "device:JKT",
                                              "positions": {"pop:BDG": {"x": 300, "y": 0}}})
    view = client.get("/api/topology/view", params={"level": "device", "pop": "JKT"}).json()
    assert view["layout_key"] == "device:JKT" and view["layout"]["pop:BDG"] == {"x": 300.0, "y": 0.0}


def test_should_forget_layout_and_old_device_positions_when_view_is_reset(client, network, db):
    client.put("/api/topology/layouts", json={"view_key": "device:JKT", "positions": {"pop:BDG": {"x": 1, "y": 2}}})
    assert client.delete("/api/topology/layouts", params={"view_key": "device:JKT"}).status_code == 204
    view = client.get("/api/topology/view", params={"level": "device", "pop": "JKT"}).json()
    device = next(n for n in view["nodes"] if n["kind"] == "device" and n["label"] == "JKT-NDC-PE1")
    assert view["layout"] == {} and (device["x"], device["y"]) == (None, None)


def test_should_return_map_layout_when_saved_for_view(client, network):
    client.put("/api/topology/layouts", json={"view_key": "device:JKT", "positions": {"pop:BDG": {"x": 300, "y": 0}}})
    response = client.put("/api/topology/layouts", json={"view_key": "map:device:JKT",
                                                          "positions": {"dev:1": {"lat": -6.2, "lon": 106.8}}})
    assert response.status_code == 204
    view = client.get("/api/topology/view", params={"level": "device", "pop": "JKT"}).json()
    assert view["map_layout"] == {"dev:1": {"lat": -6.2, "lon": 106.8}}
    assert view["layout"] == {"pop:BDG": {"x": 300.0, "y": 0.0}}


def test_should_keep_device_coordinates_when_map_layout_saved(client, network, db):
    jkt = db.scalars(select(m.Pop).where(m.Pop.code == "JKT")).one()
    jkt.latitude, jkt.longitude = -6.1, 106.7
    device = db.scalars(select(m.Device).where(m.Device.hostname == "JKT-NDC-PE1")).one()
    db.add(m.DeviceLocation(device_id=device.id, latitude=-6.15, longitude=106.75))
    db.commit()
    client.put("/api/topology/layouts", json={"view_key": "map:device:JKT",
                                              "positions": {f"dev:{device.id}": {"lat": -6.3, "lon": 106.9}}})
    db.expire_all()
    location = db.get(m.DeviceLocation, device.id)
    assert (location.latitude, location.longitude, jkt.latitude, jkt.longitude) == (-6.15, 106.75, -6.1, 106.7)


@pytest.mark.parametrize("point", [{"lat": 91, "lon": 106}, {"lat": -6, "lon": -181}, {"x": 1, "y": 2}])
def test_should_return_422_when_map_position_out_of_range(client, network, point):
    response = client.put("/api/topology/layouts", json={"view_key": "map:device:JKT", "positions": {"dev:1": point}})
    assert response.status_code == 422


@pytest.mark.parametrize("view_key", ["map:device:JKT", "device:JKT"])
def test_should_return_422_when_layout_point_has_extra_fields(client, network, view_key):
    point = {"x": 1, "y": 2, "lat": -6, "lon": 106}
    response = client.put("/api/topology/layouts", json={"view_key": view_key, "positions": {"dev:1": point}})
    assert response.status_code == 422
