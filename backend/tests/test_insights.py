"""Advanced dashboard (Phase 8): trends per area, services & validation, data health, area hotspot map."""

from datetime import datetime, timedelta, timezone

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
def net(db):
    """JKT (Jabodetabek, located) and SBY (Jawa Timur, no coordinates); one hot link JKT—SBY, one quiet in JKT."""
    jkt = m.Pop(code="JKT", name="Jakarta", region="Jabodetabek", latitude=-6.2, longitude=106.8)
    sby = m.Pop(code="SBY", name="Surabaya", region="Jawa Timur")
    db.add_all([jkt, sby])
    db.flush()
    dev = {name: m.Device(hostname=name, pop_id=pop.id, role="pe", loopback_ipv4=lo, isis_net=net)
           for name, pop, lo, net in (("JKT-PE1", jkt, "10.0.0.1", "49.1"), ("JKT-PE2", jkt, "10.0.0.2", None),
                                      ("SBY-PE1", sby, None, None))}
    db.add_all(dev.values())
    db.flush()
    hot = m.Link(name="JKT-SBY", a_device_id=dev["JKT-PE1"].id, b_device_id=dev["SBY-PE1"].id, capacity_gbps=10,
                 current_gbps=9.5, discovery_source="lldp")
    quiet = m.Link(name="JKT-JKT", a_device_id=dev["JKT-PE1"].id, b_device_id=dev["JKT-PE2"].id, capacity_gbps=100,
                   current_gbps=10, discovery_source="config")
    db.add_all([hot, quiet])
    db.flush()
    today = datetime.now(timezone.utc).replace(hour=12, minute=0, second=0, microsecond=0)
    for day in range(10):
        db.add(m.UtilizationSample(link_id=hot.id, ts=today - timedelta(days=day), in_gbps=9 - day * 0.1, out_gbps=1))
    db.commit()
    return {"dev": dev, "hot": hot, "quiet": quiet}


def test_should_sum_daily_peaks_per_area_when_building_trends(client, net):
    body = client.get("/api/dashboard/trends").json()
    assert len(body["network"]["series"]) == 10
    areas = {a["label"]: a for a in body["areas"]}
    assert set(areas) == {"Jabodetabek", "Jawa Timur"}
    assert areas["Jawa Timur"]["series"][-1]["peak_gbps"] == 9.0


def test_should_list_failing_services_as_draft_mops(client, db, net):
    db.add(m.Customer(id=1, name="X", account_no="A1"))
    db.add(m.Service(customer_id=1, circuit_id="BAD-1", service_type="l2vpn", bandwidth_mbps=100, status="planned"))
    db.commit()
    body = client.get("/api/dashboard/services").json()
    assert body["by_type"] == {"l2vpn": 1} and [d["circuit_id"] for d in body["draft_mops"]] == ["BAD-1"]


def test_should_count_routers_missing_loopback_and_isis_in_data_health(client, net):
    checks = {c["key"]: c for c in client.get("/api/dashboard/data-health").json()["checks"]}
    assert (checks["routers_without_loopback"]["count"], checks["routers_without_isis"]["count"]) == (1, 2)
    assert checks["pops_without_coordinates"]["count"] == 1 and checks["links_without_lldp"]["count"] == 1


def test_should_mark_area_critical_and_estimate_position_when_area_has_no_located_pop(client, net):
    body = client.get("/api/dashboard/areas").json()
    areas = {a["area"]: a for a in body["areas"]}
    assert areas["Jawa Timur"]["level"] == "critical" and areas["Jawa Timur"]["position_estimated"] is True
    assert areas["Jabodetabek"]["latitude"] == -6.2 and areas["Jabodetabek"]["position_estimated"] is False
    assert [(p["code"], p["level"]) for p in body["pops"]] == [("JKT", "critical")]
