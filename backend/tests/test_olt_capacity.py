"""OLT GPON port occupancy and uplink utilization, and the per-device port/capacity detail."""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import models as m
from app.config import settings
from app.database import get_db
from app.main import app
from app.services.olt import PonUsage, is_pon_port, onts_online


@pytest.fixture
def client(db):
    app.dependency_overrides[get_db] = lambda: db
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


def _olt(db, pon_online: list[int], uplink_gbps: float, uplink_capacity: float = 10.0) -> m.Device:
    """An OLT with one PON port per entry (ONTs online; 0 = empty port) and one uplink to an AGG."""
    pop = m.Pop(code="BKS", name="Bekasi")
    db.add(pop)
    db.flush()
    olt = m.Device(hostname="BKS-CKB-OLT1-ZTE", pop_id=pop.id, role="olt")
    agg = m.Device(hostname="BKS-CKB-ME1-ZTE", pop_id=pop.id, role="agg")
    db.add_all([olt, agg])
    db.flush()
    for i, online in enumerate(pon_online, start=1):
        db.add(m.Interface(device_id=olt.id, name=f"Slot1/Port{i}", speed_gbps=2.5, admin_status="up",
                           oper_status="up" if online else "down",
                           description=f"FoCut:0 online:{online} DyingGasp:0 Deactivated"))
    up = m.Interface(device_id=olt.id, name="xgei_1/10/1", speed_gbps=10, admin_status="up", oper_status="up")
    agg_port = m.Interface(device_id=agg.id, name="xgei-0/1/0/1", speed_gbps=10, admin_status="up", oper_status="up")
    db.add_all([up, agg_port])
    db.flush()
    db.add(m.Link(name="olt-uplink", a_device_id=olt.id, a_interface_id=up.id, b_device_id=agg.id,
                  b_interface_id=agg_port.id, capacity_gbps=uplink_capacity, current_gbps=uplink_gbps))
    db.commit()
    return olt


def test_should_read_pon_ports_and_onts_when_description_has_ont_counters():
    assert is_pon_port("Slot2/Port6", "FoCut:0 online:18 DyingGasp:4 Deactivate")
    assert not is_pon_port("xgei_1/10/4", None)
    assert onts_online("FoCut:0 online:18 DyingGasp:4") == 18


def test_should_leave_disabled_pon_ports_out_when_counting_occupancy():
    usage = PonUsage()
    usage.add("up", "up", "online:5")
    usage.add("up", "down", "online:0")
    usage.add("down", "down", "online:0")
    assert (usage.total, usage.used, usage.onts) == (2, 1, 5)


def test_should_measure_pon_port_against_split_ratio_when_ports_fill_with_onts(monkeypatch):
    monkeypatch.setattr(settings, "pon_max_onts", 64)
    usage = PonUsage()
    for online in (10, 48, 60, 3):
        usage.add("up", "up", f"online:{online}")
    assert (usage.busiest_onts, usage.busiest_pct, usage.hot_ports) == (60, 93.8, 2)  # 48/64 = 75 %, 60/64 = 93.8 %


def test_should_list_olt_when_a_gpon_port_reaches_threshold(db, client, monkeypatch):
    monkeypatch.setattr(settings, "pon_max_onts", 64)
    _olt(db, pon_online=[10, 50, 8, 0], uplink_gbps=2.0)  # 50 ONTs on a 1:64 port = 78.1 %
    row = client.get("/api/capacity/olts").json()["rows"][0]
    assert (row["busiest_pon_pct"], row["pon_hot_ports"], row["pon_level"], row["onts_online"]) == (78.1, 1, "warning", 68)


def test_should_list_olt_when_uplink_utilization_reaches_threshold(db, client):
    _olt(db, pon_online=[10, 0, 0, 0], uplink_gbps=9.5)
    row = client.get("/api/capacity/olts").json()["rows"][0]
    assert (row["uplink_utilization_pct"], row["uplink_level"]) == (95.0, "critical")


def test_should_skip_olt_when_gpon_and_uplink_are_below_threshold(db, client):
    _olt(db, pon_online=[10, 0, 0, 0], uplink_gbps=2.0)
    assert client.get("/api/capacity/olts").json()["total"] == 0


def test_should_show_link_utilization_on_its_port_when_device_detail_is_opened(db, client):
    olt = _olt(db, pon_online=[10, 0], uplink_gbps=8.0)
    body = client.get(f"/api/capacity/devices/{olt.id}").json()
    uplink = next(p for p in body["ports"] if p["name"] == "xgei_1/10/1")
    assert (uplink["link"]["peer"], uplink["link"]["utilization_pct"], uplink["link"]["level"]) == ("BKS-CKB-ME1-ZTE", 80.0, "warning")
    assert (body["pon"]["total"], body["pon"]["busiest_onts"]) == (2, 10)


def test_should_lay_out_olt_ports_as_uplinks_and_pon_slots_when_device_is_an_olt(db, client):
    olt = _olt(db, pon_online=[10, 0, 5], uplink_gbps=8.0)
    db.add(m.Interface(device_id=olt.id, name="Slot2/Port1", admin_status="up", oper_status="up", description="online:3"))
    db.add(m.Interface(device_id=olt.id, name="meth0", admin_status="up", oper_status="up"))
    db.commit()
    body = client.get(f"/api/capacity/devices/{olt.id}").json()
    names = {p["id"]: p["name"] for p in body["ports"]}
    layout = body["olt_layout"]
    assert [names[i] for i in layout["uplinks"]] == ["xgei_1/10/1"]  # management port left out
    assert [(s["label"], [names[i] for i in s["ports"]]) for s in layout["slots"]] == [
        ("SLOT 1", ["Slot1/Port1", "Slot1/Port2", "Slot1/Port3"]), ("SLOT 2", ["Slot2/Port1"])]


def test_should_show_pon_bandwidth_when_port_rate_was_synced(db, client):
    olt = _olt(db, pon_online=[10], uplink_gbps=1.0)
    pon = db.scalar(select(m.Interface).where(m.Interface.name == "Slot1/Port1"))
    pon.in_gbps, pon.out_gbps = 0.5, 2.0  # 2.0 of 2.5G downstream
    db.commit()
    port = next(p for p in client.get(f"/api/capacity/devices/{olt.id}").json()["ports"] if p["name"] == "Slot1/Port1")
    assert (port["utilization_pct"], port["utilization_level"]) == (80.0, "warning")


def test_should_recognise_dasan_pon_ports_when_they_are_the_2_5g_panel_ports():
    assert is_pon_port("port36-FX-1000", "PON Port OLT", 2.5)
    assert is_pon_port("port1-FX-1000", "FAT06-TBT-F05-D04", 2.488)
    assert not is_pon_port("port13-FX-1000", None, 1.0)  # a 1G uplink on the same panel
    assert not is_pon_port("port42-FX-10G", None, 10.0)


def test_should_report_unknown_occupancy_when_pon_ports_have_no_ont_counters():
    usage = PonUsage()
    usage.add("up", "up", "PON Port OLT")
    assert (usage.total, usage.used, usage.busiest_pct) == (1, 1, None)


def test_should_lay_out_dasan_pon_ports_on_one_panel_when_device_is_a_dasan_olt(db, client):
    pop = m.Pop(code="JKT", name="Jakarta")
    db.add(pop)
    db.flush()
    olt = m.Device(hostname="OLT3-V8240-JKT-CRV", pop_id=pop.id, role="olt")
    db.add(olt)
    db.flush()
    for name, speed in (("port10-FX-1000", 2.5), ("port2-FX-1000", 2.5), ("port42-FX-10G", 10.0), ("br1", 0.1)):
        db.add(m.Interface(device_id=olt.id, name=name, speed_gbps=speed, admin_status="up", oper_status="up"))
    db.commit()
    body = client.get(f"/api/capacity/devices/{olt.id}").json()
    names = {p["id"]: p["name"] for p in body["ports"]}
    layout = body["olt_layout"]
    assert [(s["label"], [names[i] for i in s["ports"]]) for s in layout["slots"]] == [("PON", ["port2-FX-1000", "port10-FX-1000"])]
    assert [names[i] for i in layout["uplinks"]] == ["port42-FX-10G"]
    assert body["pon"]["busiest_pct"] is None


def test_should_answer_404_when_device_detail_is_missing(client):
    assert client.get("/api/capacity/devices/999").status_code == 404
