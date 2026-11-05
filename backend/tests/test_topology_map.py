"""Map positions for the topology: area centres from POP coordinates, estimates for POPs without any."""

import pytest
from fastapi.testclient import TestClient

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
    plg = m.Pop(code="PLG", name="Palembang", latitude=-2.95, longitude=104.73)
    pbm = m.Pop(code="XQA", name="XQA", region="Sumatera")  # not a known city, no coordinates, linked to PLG
    llg = m.Pop(code="XQB", name="XQB", region="Sumatera")  # not a known city, no coordinates, no links
    lhk = m.Pop(code="LSM", name="LSM")  # known city (Lhokseumawe), no coordinates
    db.add_all([plg, pbm, llg, lhk])
    db.flush()
    a = m.Device(hostname="PLG-NDC-ME1", pop_id=plg.id, role="agg")
    b = m.Device(hostname="XQA-XQA-ME1", pop_id=pbm.id, role="agg")
    c = m.Device(hostname="XQB-XQB-OLT1", pop_id=llg.id, role="olt")
    db.add_all([a, b, c])
    db.flush()
    db.add(m.Link(name="plg-pbm", a_device_id=a.id, b_device_id=b.id, capacity_gbps=10))
    db.commit()


def test_should_center_area_on_its_located_pops_when_drawing_the_map(client, network):
    area = next(n for n in client.get("/api/topology/view?level=area").json()["nodes"] if n["label"] == "Sumatera")
    assert (area["lat"], area["lon"]) == (-2.95, 104.73) and area["pops_located"] == 1


def test_should_estimate_unlocated_pop_near_its_linked_neighbour(client, network):
    nodes = {n.get("code", n["label"]): n for n in client.get("/api/topology/view?level=pop&area=Sumatera").json()["nodes"]}
    assert nodes["PLG"]["estimated"] is False
    assert nodes["XQA"]["estimated"] is True
    assert abs(nodes["XQA"]["lat"] - -2.95) < 0.1 and abs(nodes["XQA"]["lon"] - 104.73) < 0.1


def test_should_place_unlocated_pop_at_its_city_centre_when_code_names_a_city(client, network):
    lsm = next(n for n in client.get("/api/topology/view?level=pop&area=Sumatera").json()["nodes"] if n.get("code") == "LSM")
    assert lsm["estimated"] is True and (lsm["lat"], lsm["lon"]) == (5.18, 97.15)


def test_should_ring_unlinked_unlocated_pop_around_area_center(client, network):
    xqb = next(n for n in client.get("/api/topology/view?level=pop&area=Sumatera").json()["nodes"] if n.get("code") == "XQB")
    assert xqb["estimated"] is True and xqb["lat"] is not None


def test_should_place_device_at_its_own_site_and_others_around_their_pop(client, network, db):
    from sqlalchemy import select

    plg_device = db.scalar(select(m.Device).where(m.Device.hostname == "PLG-NDC-ME1"))
    db.add(m.Device(hostname="PLG-AAL-OLT1-FH", pop_id=plg_device.pop_id, role="olt"))
    db.add(m.DeviceLocation(device_id=plg_device.id, latitude=-2.93, longitude=104.70, placemark="ODC Alang-Alang Lebar"))
    db.commit()
    nodes = {n.get("code", n["label"]): n for n in client.get("/api/topology/view?level=device&pop=PLG").json()["nodes"]}
    own, ringed = nodes["PLG-NDC-ME1"], nodes["PLG-AAL-OLT1-FH"]
    assert (own["lat"], own["lon"], own["located"], own["estimated"]) == (-2.93, 104.70, True, False)
    assert ringed["located"] is False and ringed["estimated"] is True
    assert abs(ringed["lat"] - -2.95) < 0.02 and abs(ringed["lon"] - 104.73) < 0.02  # near the POP point
    assert nodes["XQA"]["kind"] == "external_pop" and nodes["XQA"].get("lat") is None  # no coordinates, no city


def test_should_have_every_city_centre_inside_its_own_area():
    from app.services.areas import CITIES, CITY_CENTERS, in_area

    area_of_city = {name: area for name, area in CITIES.values()}
    misplaced = [(city, spot) for city, spot in CITY_CENTERS.items()
                 if city in area_of_city and in_area(area_of_city[city], *spot) is False]
    assert misplaced == []
    assert set(CITY_CENTERS) - set(area_of_city) <= {"singapore", "hong kong"}
