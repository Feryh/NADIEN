"""Inventory > Router, ticked rows: move many devices to one POP and/or put their POPs in one area."""

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
    pops = {code: m.Pop(code=code, name=code) for code in ("UNASSIGNED", "BDG", "CMH")}
    db.add_all(pops.values())
    db.flush()
    devices = [m.Device(hostname=f"DEV{i}", pop_id=pops["UNASSIGNED"].id, role="olt") for i in range(3)]
    devices.append(m.Device(hostname="CMH-OLT1", pop_id=pops["CMH"].id, role="olt"))
    db.add_all(devices)
    db.commit()
    return pops, devices


def test_should_move_ticked_devices_to_one_pop_and_lock_them_there(client, network, db):
    pops, devices = network
    body = client.patch("/api/devices", json={"device_ids": [d.id for d in devices[:2]], "pop_id": pops["BDG"].id}).json()
    db.expire_all()
    assert body["devices_moved"] == 2
    assert [(d.pop_id, d.pop_locked) for d in devices[:2]] == [(pops["BDG"].id, True)] * 2


def test_should_set_the_area_of_the_ticked_devices_pops_and_count_every_device_that_follows(client, network, db):
    pops, devices = network
    body = client.patch("/api/devices", json={"device_ids": [devices[3].id], "area": "Jawa Barat & Banten"}).json()
    db.expire_all()
    assert (pops["CMH"].region, body["pops_area_changed"], body["devices_in_changed_pops"]) == ("Jawa Barat & Banten", ["CMH"], 1)


def test_should_leave_the_unassigned_pop_without_an_area_and_report_it(client, network, db):
    pops, devices = network
    body = client.patch("/api/devices", json={"device_ids": [devices[0].id], "area": "Sumatera"}).json()
    db.expire_all()
    assert (body["skipped_pops"], pops["UNASSIGNED"].region) == (["UNASSIGNED"], None)


def test_should_move_first_then_set_the_area_of_the_pop_moved_to(client, network, db):
    pops, devices = network
    client.patch("/api/devices", json={"device_ids": [devices[0].id], "pop_id": pops["BDG"].id, "area": "Jawa Barat & Banten"})
    db.expire_all()
    assert pops["BDG"].region == "Jawa Barat & Banten"


def test_should_answer_422_when_neither_a_pop_nor_an_area_is_given(client, network):
    _, devices = network
    assert client.patch("/api/devices", json={"device_ids": [devices[0].id]}).status_code == 422


def test_should_answer_404_and_change_nothing_when_a_device_does_not_exist(client, network, db):
    pops, devices = network
    response = client.patch("/api/devices", json={"device_ids": [devices[0].id, 999_999], "pop_id": pops["BDG"].id})
    db.expire_all()
    assert response.status_code == 404 and devices[0].pop_id == pops["UNASSIGNED"].id
