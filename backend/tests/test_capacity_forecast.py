"""90-day trend forecast (per link and per node), hotspots and the device summary."""

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app import models as m
from app.database import get_db
from app.main import app
from app.services.capacity import calculate, fit_trend, hotspot_level


@pytest.fixture
def client(db):
    app.dependency_overrides[get_db] = lambda: db
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


def _network(db, peaks: list[float], capacity: float = 100.0):
    """Two devices, one link, one sample per day ending today with the given daily peaks."""
    pop = m.Pop(code="JKT", name="Jakarta")
    db.add(pop)
    db.flush()
    a = m.Device(hostname="JKT-A-ME1", pop_id=pop.id, role="agg")
    b = m.Device(hostname="JKT-B-PE1", pop_id=pop.id, role="pe")
    db.add_all([a, b])
    db.flush()
    link = m.Link(name="a<>b", a_device_id=a.id, b_device_id=b.id, capacity_gbps=capacity, current_gbps=peaks[-1])
    db.add(link)
    db.flush()
    today = datetime.now(timezone.utc).replace(hour=12, minute=0, second=0, microsecond=0)
    for i, peak in enumerate(peaks):
        ts = today - timedelta(days=len(peaks) - 1 - i)
        db.add(m.UtilizationSample(link_id=link.id, ts=ts, in_gbps=peak, out_gbps=peak / 2))
    db.commit()
    return link


def test_should_start_forecast_from_trend_peak_level_when_newest_day_is_unusually_quiet():
    # 60 days growing 1G/day around 20..79G with a weekly +5G peak; the last day dips to 10G.
    points = [(float(d), 20 + d + (5 if d % 7 == 0 else 0)) for d in range(60)]
    points[-1] = (59.0, 10.0)
    fit = fit_trend(points)
    result = calculate(200, 10, growth_abs=fit.growth_per_month, growth_source="trend", trend=fit)
    assert result.forecast_method == "trend" and result.forecast_basis_gbps > 70  # not the 10G quiet day
    assert result.months_to_threshold < calculate(200, 10, growth_abs=fit.growth_per_month).months_to_threshold


def test_should_fall_back_to_current_when_history_is_shorter_than_two_weeks(db, client):
    _network(db, [30, 31, 32, 33, 34])
    link = client.get("/api/capacity/links").json()[0]
    assert link["forecast_method"] == "current" and link["forecast_basis_gbps"] == 34
    assert link["growth_gbps_per_month"] == pytest.approx(30.4, rel=0.01)  # growth still from the trend


def test_should_forecast_per_device_and_pop_when_links_have_90_days_of_peaks(db, client):
    _network(db, [20 + 0.5 * d for d in range(90)])
    devices = client.get("/api/capacity/nodes?level=device").json()
    pops = client.get("/api/capacity/nodes?level=pop").json()
    assert devices["total"] == 2 and pops["total"] == 1
    node = pops["nodes"][0]
    assert node["label"] == "JKT" and node["forecast_method"] == "trend" and node["trend_days"] == 89
    assert node["growth_gbps_per_month"] == pytest.approx(15.2, rel=0.02)


def test_should_flag_links_at_70_and_90_percent_when_listing_hotspots(db, client):
    _network(db, [95.0], capacity=100)
    body = client.get("/api/capacity/hotspots").json()
    assert body["thresholds"] == {"warning_pct": 70.0, "critical_pct": 90.0, "suspect_pct": 150.0}
    assert body["links"]["rows"][0]["level"] == "critical" and body["links"]["counts"]["critical"] == 1
    assert (hotspot_level(69.9), hotspot_level(70), hotspot_level(90)) == ("normal", "warning", "critical")


def test_should_name_ports_and_flag_bundle_when_listing_hot_links(db, client):
    link = _network(db, [80.0], capacity=100)
    a = m.Interface(device_id=link.a_device_id, name="Bundle-Ether10", if_type="bundle")
    b = m.Interface(device_id=link.b_device_id, name="Eth-Trunk10", if_type="bundle")
    db.add_all([a, b])
    db.flush()
    link.a_interface_id, link.b_interface_id = a.id, b.id
    db.commit()
    row = client.get("/api/capacity/hotspots").json()["links"]["rows"][0]
    assert (row["a_port"], row["b_port"], row["bundle"], row["level"]) == ("Bundle-Ether10", "Eth-Trunk10", True, "warning")


def test_should_mark_capacity_suspect_when_utilization_is_far_above_100_percent(db, client):
    _network(db, [7.6], capacity=0.01)  # 10M per SNMP on a link carrying 7.6G
    row = client.get("/api/capacity/hotspots").json()["links"]["rows"][0]
    assert row["level"] == "suspect"


def test_should_rank_devices_by_port_occupancy_when_listing_hotspots(db, client):
    link = _network(db, [10.0])
    ports = [m.Interface(device_id=link.a_device_id, name=f"ge-{i}", oper_status="up" if i else "down") for i in range(4)]
    ports.append(m.Interface(device_id=link.b_device_id, name="ge-0", oper_status="down"))
    db.add_all(ports)
    db.flush()
    db.add_all(m.ObserviumPort(interface_id=p.id, observium_port_id=p.id, observium_hostname="x") for p in ports)
    db.commit()
    rows = client.get("/api/capacity/hotspots").json()["devices"]["rows"]
    assert rows[0]["hostname"] == "JKT-A-ME1" and rows[0]["used_pct"] == 75.0 and rows[0]["level"] == "warning"


def test_should_count_devices_per_category_including_csr_when_summarising(db, client):
    pop = m.Pop(code="BLI", name="Bangli")
    db.add(pop)
    db.flush()
    db.add_all([m.Device(hostname="BAL-BA-BLI-0004-ME1-ZTE", pop_id=pop.id, role="csr"),
                m.Device(hostname="BLI-X-OLT1-HW", pop_id=pop.id, role="olt"),
                m.Device(hostname="BLI-X-OLT2-HW", pop_id=pop.id, role="olt")])
    db.commit()
    body = client.get("/api/inventory/devices/summary").json()
    by_cat = {c["category"]: c for c in body["categories"]}
    assert body["total"] == 3 and by_cat["olt"]["devices"] == 2 and by_cat["csr"]["devices"] == 1
    assert by_cat["csr"]["areas"] == {"Bali & Nusa Tenggara": 1}
