"""Topology search, manual POP area, the daily gitweb scheduler and DB-backed job state."""

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from app import models as m
from app.collectors import scheduler
from app.collectors.runner import JobRunner
from app.config import settings
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
    jkt = m.Pop(code="JKT", name="Jakarta")
    sgm = m.Pop(code="SGM", name="Sungguminasa")
    db.add_all([jkt, sgm])
    db.flush()
    db.add_all([m.Device(hostname="JKT-ABW-OLT1-HW", pop_id=jkt.id, role="olt"),
                m.Device(hostname="JKT-ABW-ME1-NCS540", pop_id=jkt.id, role="agg"),
                m.Device(hostname="SUL-SN-SGM-0078-ME1-ZTE", pop_id=sgm.id, role="csr")])
    db.commit()
    return {"jkt": jkt, "sgm": sgm}


def test_should_rank_exact_then_prefix_matches_when_searching_hostnames(client, network):
    body = client.get("/api/topology/search?q=jkt-abw").json()
    assert [d["hostname"] for d in body["devices"]] == ["JKT-ABW-OLT1-HW", "JKT-ABW-ME1-NCS540"]
    assert body["pops"] == []


def test_should_return_pop_with_area_when_searching_pop_code(client, network):
    body = client.get("/api/topology/search?q=SGM").json()
    assert body["pops"][0]["code"] == "SGM" and body["pops"][0]["devices"] == 1
    assert body["devices"][0]["category"] == "csr"


def test_should_override_automatic_area_when_pop_area_is_set_by_hand(client, network, db):
    resp = client.put(f"/api/pops/{network['jkt'].id}/area", json={"area": "Jawa Barat & Banten"})
    assert resp.json()["area"] == "Jawa Barat & Banten"
    assert db.get(m.Pop, network["jkt"].id).region == "Jawa Barat & Banten"
    cleared = client.put(f"/api/pops/{network['jkt'].id}/area", json={"area": ""}).json()
    assert cleared["region"] is None and cleared["area"] == "Jabodetabek"  # back to automatic


def test_should_offer_standard_areas_when_listing_area_options(client, network):
    areas = client.get("/api/areas/options").json()["areas"]
    assert "Sulawesi" in areas and "Belum dipetakan" not in areas


@pytest.fixture
def sched(db, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "gitweb_url", "http://gitweb.test/gitweb/")
    monkeypatch.setattr(settings, "gitweb_daily_at", "02:30")
    factory = sessionmaker(bind=db.get_bind(), autoflush=False, expire_on_commit=False)
    return factory, JobRunner(session_factory=factory, state_file=tmp_path / "job.json")


def _at(hour: int, minute: int) -> datetime:
    return datetime(2026, 11, 3, hour, minute, tzinfo=timezone(timedelta(hours=7)))


def test_should_start_gitweb_once_per_day_when_schedule_time_has_passed(sched):
    factory, runner = sched
    calls = []
    job = lambda db, progress: calls.append(1) or {}  # noqa: E731
    first = scheduler.tick(runner, factory, now=_at(2, 31), start_job=job)
    runner.wait(5)
    second = scheduler.tick(runner, factory, now=_at(9, 0), start_job=job)
    assert first is not None and second is None and calls == [1]


def test_should_wait_when_schedule_time_has_not_come(sched):
    factory, runner = sched
    assert scheduler.tick(runner, factory, now=_at(2, 29), start_job=lambda db, p: {}) is None


def test_should_retry_next_minute_when_another_sync_is_running(sched, db):
    factory, runner = sched
    now = datetime.now(timezone.utc)
    db.add(m.CollectorJob(id="busy1", kind="observium", status="running", phase="ports", started_at=now, heartbeat_at=now))
    db.commit()
    assert scheduler.tick(runner, factory, now=_at(3, 0), start_job=lambda db, p: {}) is None
    assert db.scalars(select(m.ScheduledRun)).all() == []  # the day is not claimed yet


def test_should_report_job_lost_when_its_heartbeat_stopped(sched, db):
    _, runner = sched
    old = datetime.now(timezone.utc) - timedelta(minutes=10)
    db.add(m.CollectorJob(id="dead1", kind="gitweb", status="running", phase="configs", started_at=old, heartbeat_at=old))
    db.commit()
    assert runner.running() is None
    job = runner.current()
    assert job["status"] == "failed" and job["error"].startswith("lost")
