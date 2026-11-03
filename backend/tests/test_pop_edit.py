"""Editing POPs: readable duplicate-code errors, the UNASSIGNED parking POP, and merging duplicates."""

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
def pops(db):
    jkt = m.Pop(code="JKT", name="Jakarta", region="Jabodetabek")
    jkts = m.Pop(code="JKTS", name="Jakarta Selatan", latitude=-6.26, longitude=106.81)
    parked = m.Pop(code="UNASSIGNED", name="UNASSIGNED")
    db.add_all([jkt, jkts, parked])
    db.flush()
    db.add_all([m.Device(hostname="OLT2-MA5800-JKTS-TND", pop_id=jkts.id, role="olt"),
                m.Device(hostname="PE1-ATN910-SERUI", pop_id=parked.id)])
    db.commit()
    return {"jkt": jkt, "jkts": jkts, "parked": parked}


def test_should_name_the_existing_pop_when_new_code_is_already_used(client, pops):
    resp = client.patch(f"/api/pops/{pops['jkts'].id}", json={"code": "JKT"})
    assert resp.status_code == 409
    assert resp.json()["detail"] == f'code "JKT" sudah dipakai Pop lain (id {pops["jkt"].id})'


def test_should_refuse_renaming_unassigned_when_code_changes(client, pops):
    resp = client.patch(f"/api/pops/{pops['parked'].id}", json={"code": "NDC", "region": "Jabodetabek"})
    assert resp.status_code == 422 and "UNASSIGNED" in resp.json()["detail"]


def test_should_allow_other_fields_when_editing_unassigned(client, pops):
    assert client.patch(f"/api/pops/{pops['parked'].id}", json={"name": "Parkir"}).status_code == 200


def test_should_refuse_deleting_unassigned(client, pops):
    assert client.delete(f"/api/pops/{pops['parked'].id}").status_code == 422


def test_should_move_devices_and_fill_details_when_merging_duplicate_pop(client, pops, db):
    body = client.post(f"/api/pops/{pops['jkts'].id}/merge", json={"target_code": "jkt"}).json()
    assert body["into"] == "JKT" and body["moved"] == {"devices.pop_id": 1}
    device = db.scalar(select(m.Device).where(m.Device.hostname == "OLT2-MA5800-JKTS-TND"))
    assert device.pop_id == pops["jkt"].id
    assert db.get(m.Pop, pops["jkts"].id) is None
    assert db.get(m.Pop, pops["jkt"].id).latitude == -6.26 and db.get(m.Pop, pops["jkt"].id).region == "Jabodetabek"


def test_should_refuse_merging_unassigned_into_one_city(client, pops):
    resp = client.post(f"/api/pops/{pops['parked'].id}/merge", json={"target_code": "JKT"})
    assert resp.status_code == 422
