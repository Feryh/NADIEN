"""90-day trend forecast (per link and per node), hotspots and the device summary."""

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app import models as m
from app.database import get_db
from app.main import app
from app.services.capacity import (calculate, committed_pct, fit_trend, hotspot_level, read_on_wider_end,
                                   utilization_pct)


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


def test_should_count_no_traffic_on_a_down_link_when_listing_link_capacity(db, client):
    link = _network(db, [4.7] * 30, capacity=0.01)  # an old 10M pairing replaced by the right port
    link.status = "down"
    db.commit()
    row = client.get("/api/capacity/links").json()[0]
    assert (row["current_gbps"], row["utilization_pct"], row["growth_trends"]["7"]) == (0.0, 0.0, None)


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


def test_should_add_reserved_to_current_when_computing_committed_pct():
    assert committed_pct(2, 3, 10) == 50.0


def test_should_return_zero_when_capacity_is_zero_for_committed_pct():
    assert committed_pct(2, 3, 0) == 0.0


def test_should_ignore_reserved_when_computing_utilization_pct():
    assert utilization_pct(2, 10) == 20.0


def test_should_return_zero_when_capacity_is_none_for_utilization_pct():
    assert utilization_pct(2, None) == 0.0


def test_should_leave_one_day_glitch_out_of_trend_and_list_it_when_showing_link_detail(client, db):
    peaks = [40.0] * 20
    peaks[15] = 150.0  # a 100G link "peaking" at 150G for one day between 40G days
    link = _network(db, peaks)
    detail = client.get(f"/api/capacity/links/{link.id}").json()
    assert [d[:10] for d in detail["spike_days"]] == [detail["history"][15]["ts"][:10]]
    assert abs(detail["growth_trends"]["30"]) < 0.01  # flat without the glitch


def _bundle_link(db, narrow_monitored: bool, narrow_rate: float = 31.0):
    """A 100G port facing a 300G bundle; the link's daily peaks (130G) came from the bundle."""
    link = _network(db, [130.0] * 3, capacity=100.0)
    narrow = m.Interface(device_id=link.a_device_id, name="smartgroup99", speed_gbps=100.0, in_gbps=narrow_rate)
    wide = m.Interface(device_id=link.b_device_id, name="Eth-Trunk2", speed_gbps=300.0, in_gbps=131.0)
    db.add_all([narrow, wide])
    db.flush()
    link.a_interface_id, link.b_interface_id = narrow.id, wide.id
    db.add(m.ObserviumPort(interface_id=wide.id, observium_port_id=2, observium_hostname="me", source="main"))
    if narrow_monitored:
        db.add(m.ObserviumPort(interface_id=narrow.id, observium_port_id=1, observium_hostname="bng", source="main"))
    db.commit()
    return link


def test_should_mark_link_suspect_when_traffic_above_capacity_is_read_only_on_the_wider_end(db, client):
    _bundle_link(db, narrow_monitored=False)
    row = client.get("/api/capacity/links").json()[0]
    assert (row["measured_on_wider_end"], row["level"]) == (True, "suspect")


def _vmx_behind_a_bundle(db):
    """CGW-BRAS-CORP-NIX-JKT ae1 <> NEW-VMX-ROUTE-INJECTOR-JKT-NIX ge-0/0/0: a 1G vMX port Observium does not watch,
    so the link's traffic was read on ae1, 17.8 Gbps of which almost none goes to the vMX."""
    link = _network(db, [17.9, 17.6, 17.8], capacity=1.0)
    narrow = m.Interface(device_id=link.b_device_id, name="ge-0/0/0", speed_gbps=1.0)
    wide = m.Interface(device_id=link.a_device_id, name="ae1", if_type="bundle", speed_gbps=40.0, in_gbps=17.8)
    db.add_all([narrow, wide])
    db.flush()
    link.a_interface_id, link.b_interface_id = wide.id, narrow.id
    db.add(m.ObserviumPort(interface_id=wide.id, observium_port_id=3, observium_hostname="cgw", source="main"))
    db.commit()


def test_should_report_a_link_unmeasured_when_its_wider_end_carries_more_than_the_link_could(db, client):
    _vmx_behind_a_bundle(db)
    row = client.get("/api/capacity/links").json()[0]
    assert (row["unmeasured"], row["level"], row["current_gbps"], row["utilization_pct"]) == (True, "suspect", 0.0, 0.0)
    assert row["exhaustion_date"] is None and row["growth_trends"]["7"] is None


def test_should_keep_an_unmeasured_link_out_of_the_dashboard_health_colours(db, client):
    _vmx_behind_a_bundle(db)
    assert client.get("/api/dashboard/summary").json()["link_colors"] == {}


def test_should_keep_link_critical_when_its_narrower_end_is_monitored(db, client):
    _bundle_link(db, narrow_monitored=True)
    assert client.get("/api/capacity/links").json()[0]["level"] == "critical"


def test_should_read_on_wider_end_when_the_narrower_end_counts_nothing():
    assert read_on_wider_end((100.0, 200.0), (0.0, 79.0), (True, True)) is True


def test_should_not_read_on_wider_end_when_both_ends_have_the_same_speed():
    assert read_on_wider_end((10.0, 10.0), (None, 5.0), (False, True)) is False


def test_should_keep_a_wider_end_reading_below_100_percent_out_of_suspect():
    assert hotspot_level(95.0, unverified=True) == "critical"
