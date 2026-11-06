"""A POP chosen by hand stays: sync and the hostname rules never move a locked device."""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import models as m
from app.collectors.observium.client import ObserviumClient
from app.collectors.observium.sync import sync_observium
from app.database import get_db
from app.main import app
from tests.test_large_network import _transport_with


@pytest.fixture
def client(db):
    app.dependency_overrides[get_db] = lambda: db
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


@pytest.fixture
def siak(db) -> dict[str, m.Pop]:
    """POP SAK arranged by hand, holding devices whose hostnames name the city code SIAK."""
    pops = {code: m.Pop(code=code, name=code) for code in ("SAK", "TNG")}
    db.add_all(pops.values())
    db.flush()
    db.add_all([m.Device(hostname="PE1-ATN910-SIAK-PRB", pop_id=pops["SAK"].id, pop_locked=True),
                m.Device(hostname="DS1-S5732-SIAK-PRB", pop_id=pops["SAK"].id, role="switch")])
    db.commit()
    return pops


def test_should_lock_device_pop_when_it_is_changed_in_the_inventory(client, siak, db):
    device = db.scalar(select(m.Device).where(m.Device.hostname == "DS1-S5732-SIAK-PRB"))
    assert client.patch(f"/api/devices/{device.id}", json={"pop_id": siak["TNG"].id}).status_code == 200
    db.refresh(device)
    assert device.pop_locked is True


def test_should_not_propose_moving_locked_devices_when_tidying_pops(client, siak):
    body = client.post("/api/pops/rehome").json()
    moved = [h for mv in body["moves"] for h in mv["examples"]]
    assert moved == ["DS1-S5732-SIAK-PRB"] and body["devices_kept_manual"] == 1


def test_should_lock_every_device_of_a_pop_when_following_the_manual_choice(client, siak):
    assert client.post("/api/pops/rehome/keep", params={"pop": "sak"}).json()["devices_locked"] == 1
    body = client.post("/api/pops/rehome").json()
    assert body["moves"] == [] and body["devices_kept_manual"] == 2


def test_should_keep_locked_device_in_its_pop_when_observium_syncs(db):
    unassigned = m.Pop(code="UNASSIGNED", name="UNASSIGNED")
    db.add_all([unassigned, m.Pop(code="BKS", name="Bekasi")])
    db.flush()
    db.add(m.Device(hostname="OLT2-C300-BKS-ART", pop_id=unassigned.id, role="olt", pop_locked=True))
    db.commit()
    devices = {"1": {"device_id": "1", "sysName": "OLT2-C300-BKS-ART", "os": "zte"}}
    client = ObserviumClient(base_url="http://o.test", token="t", transport=_transport_with(devices))
    report = sync_observium(db, client, {"inventory"}, discover=False)
    assert db.scalar(select(m.Device)).pop_id == unassigned.id and report.counts["devices_pop_assigned"] == 0
