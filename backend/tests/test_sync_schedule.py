"""Expected sync schedules against the collector jobs that ran (services/sync_schedule.py, /api/collectors)."""

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from app import models as m
from app.config import settings
from app.database import get_db
from app.main import app
from app.services import sync_schedule

WIB = timezone(timedelta(hours=7))
NOW = datetime(2026, 11, 26, 10, 35, tzinfo=WIB)
K8S = Path(__file__).resolve().parents[2] / "deploy" / "k8s"
DAILY_CSR = {"key": "csr", "label": "Observium CSR", "kinds": ["observium-csr"], "daily_at": "01:30"}
UTILIZATION = {"key": "util", "label": "Utilisasi", "kinds": ["observium-utilization"], "every_minutes": 10}


@pytest.fixture
def schedules(monkeypatch):
    real_status = sync_schedule.status

    def use(*specs: dict) -> None:
        monkeypatch.setattr(settings, "sync_schedules", list(specs))
        monkeypatch.setattr(settings, "schedule_timezone", "Asia/Jakarta")
        # The API reads the wall clock; the jobs below are placed around NOW, so a later day would see missed runs.
        monkeypatch.setattr(sync_schedule, "status", lambda db, now=None: real_status(db, now or NOW))

    return use


@pytest.fixture
def client(db):
    app.dependency_overrides[get_db] = lambda: db
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


def _job(db, kind: str, started: datetime, minutes: float = 5, status: str = "ok", report: dict | None = None,
         job_id: str | None = None) -> m.CollectorJob:
    started = started.astimezone(timezone.utc)  # as the runner writes it; SQLite keeps no offset
    finished = started + timedelta(minutes=minutes) if status != "running" else None
    job = m.CollectorJob(id=job_id or f"{kind[:6]}{int(started.timestamp())}"[-16:], kind=kind, status=status,
                         phase="done", started_at=started, finished_at=finished, heartbeat_at=started, report=report,
                         error="boom" if status == "failed" else None)
    db.add(job)
    db.commit()
    return job


def _row(db, key: str = "csr") -> dict:
    return next(row for row in sync_schedule.status(db, NOW)["schedules"] if row["key"] == key)


def test_should_mark_the_daily_run_ok_when_its_job_started_inside_the_window(db, schedules):
    schedules(DAILY_CSR)
    _job(db, "observium-csr", datetime(2026, 11, 26, 1, 30, 20, tzinfo=WIB))
    assert _row(db)["last"]["state"] == "ok"


def test_should_mark_the_daily_run_missed_when_no_job_started(db, schedules):
    schedules(DAILY_CSR)
    assert _row(db)["last"]["state"] == "missed"


def test_should_name_the_sync_that_held_the_runner_when_a_run_was_missed(db, schedules):
    schedules(DAILY_CSR)
    _job(db, "observium", datetime(2026, 11, 26, 1, 0, tzinfo=WIB), minutes=50, job_id="fullsync")
    assert _row(db)["last"]["blocked_by"]["id"] == "fullsync"


def test_should_name_the_sync_that_won_the_same_minute_race_when_a_run_was_missed(db, schedules):
    schedules(DAILY_CSR)
    _job(db, "observium-utilization", datetime(2026, 11, 26, 1, 30, 4, tzinfo=WIB), minutes=4, job_id="util0130")
    assert _row(db)["last"]["blocked_by"]["id"] == "util0130"


def test_should_report_failed_when_the_matching_job_failed(db, schedules):
    schedules(DAILY_CSR)
    _job(db, "observium-csr", datetime(2026, 11, 26, 1, 30, 5, tzinfo=WIB), status="failed")
    assert _row(db)["last"]["state"] == "failed"


def test_should_mark_the_run_late_when_its_job_started_after_the_window_but_before_the_next_run(db, schedules):
    schedules(DAILY_CSR)
    _job(db, "observium-csr", datetime(2026, 11, 26, 8, 46, tzinfo=WIB))
    assert _row(db)["last"]["state"] == "late"


def test_should_not_flag_data_health_when_the_latest_run_was_only_late(client, db, schedules):
    schedules(DAILY_CSR)
    _job(db, "observium-csr", datetime(2026, 11, 26, 8, 46, tzinfo=WIB))
    checks = {c["key"]: c for c in client.get("/api/dashboard/data-health").json()["checks"]}
    assert checks["sync_schedule"]["severity"] == "ok"


def test_should_keep_the_current_interval_run_due_while_it_is_inside_its_grace(db, schedules):
    schedules(UTILIZATION)
    row = _row(db, "util")
    assert row["next_due"]["due_at"].startswith("2026-11-26T10:30") and row["next_due"]["state"] == "due"


def test_should_count_a_day_of_interval_runs_when_every_slot_ran(db, schedules):
    schedules(UTILIZATION)
    slot = datetime(2026, 11, 25, 10, 40, tzinfo=WIB)
    while slot <= datetime(2026, 11, 26, 10, 20, tzinfo=WIB):
        _job(db, "observium-utilization", slot + timedelta(seconds=5), minutes=4)
        slot += timedelta(minutes=10)
    assert _row(db, "util")["counts"] == {"ok": 143, "late": 0, "failed": 0, "missed": 0, "running": 0}


def test_should_expect_only_the_worker_sync_all_when_no_schedules_are_configured(monkeypatch):
    monkeypatch.setattr(settings, "sync_schedules", None)
    monkeypatch.setattr(settings, "scheduler_enabled", True)
    monkeypatch.setattr(settings, "gitweb_daily_at", "02:30")
    assert [(s.key, s.kinds, s.source) for s in sync_schedule.configured()] == [
        ("sync-all", ("sync-all-scheduled",), "worker")]


def test_should_report_a_bad_schedule_setting_instead_of_failing(db, schedules):
    schedules({"key": "x", "kinds": ["observium"], "every_minutes": 10, "daily_at": "01:00"})
    assert sync_schedule.status(db, NOW)["error"]


def test_should_flag_data_health_when_the_latest_run_was_missed(client, db, schedules):
    schedules(DAILY_CSR)
    checks = {c["key"]: c for c in client.get("/api/dashboard/data-health").json()["checks"]}
    assert checks["sync_schedule"]["severity"] == "warn" and "Observium CSR" in checks["sync_schedule"]["hint"]


def test_should_list_jobs_newest_first_with_sync_all_steps(client, db):
    _job(db, "observium-utilization", datetime(2026, 11, 26, 3, 30, tzinfo=timezone.utc))
    _job(db, "sync-all-scheduled", datetime(2026, 11, 25, 19, 30, tzinfo=timezone.utc), minutes=12,
         report={"steps": [{"step": "observium", "status": "ok", "duration_s": 300, "report": {"big": "x"}}]})
    jobs = client.get("/api/collectors/jobs").json()["jobs"]
    assert [j["kind"] for j in jobs] == ["observium-utilization", "sync-all-scheduled"]
    assert jobs[1]["steps"] == [{"step": "observium", "status": "ok", "duration_s": 300}]


def test_should_serve_the_schedule_status_from_the_collectors_api(client, schedules):
    schedules(DAILY_CSR)
    body = client.get("/api/collectors/schedule").json()
    assert [row["key"] for row in body["schedules"]] == ["csr"] and body["error"] is None


def _cron_to_spec(cron: str) -> dict:
    minute, hour, *_rest = cron.split()
    if match := re.fullmatch(r"\*/(\d+)", minute):
        return {"every_minutes": int(match.group(1))}
    return {"daily_at": f"{int(hour):02d}:{int(minute):02d}"}


def test_should_declare_every_active_k8s_cronjob_in_the_expected_schedules():
    cronjobs = [doc for doc in yaml.safe_load_all((K8S / "cronjobs.yaml").read_text(encoding="utf-8"))
                if doc and doc.get("kind") == "CronJob" and not doc["spec"].get("suspend")]
    literals = yaml.safe_load((K8S / "kustomization.yaml").read_text(encoding="utf-8"))["configMapGenerator"][0]["literals"]
    declared = json.loads(next(v for v in literals if v.startswith("NADIEN_SYNC_SCHEDULES=")).split("=", 1)[1])
    by_source = {spec["source"]: spec for spec in declared}
    for job in cronjobs:
        spec = by_source[f"cronjob:{job['metadata']['name']}"]
        expected = _cron_to_spec(job["spec"]["schedule"])
        assert {k: spec.get(k) for k in expected} == expected, job["metadata"]["name"]
    assert len(declared) == len(cronjobs) + 1 and by_source["worker"]["kinds"] == ["sync-all-scheduled"]


def test_should_retry_daily_cronjobs_on_409_within_their_deadline():
    for job in yaml.safe_load_all((K8S / "cronjobs.yaml").read_text(encoding="utf-8")):
        if not job or job.get("kind") != "CronJob" or job["spec"]["schedule"].startswith("*/"):
            continue
        spec = job["spec"]["jobTemplate"]["spec"]
        script = spec["template"]["spec"]["containers"][0]["command"][-1]
        attempts = int(re.search(r"seq 1 (\d+)", script).group(1))
        sleep = int(re.search(r"sleep (\d+)", script).group(1))
        assert '[ "$code" = 409 ] || break' in script and script.rstrip().endswith('[ "$code" = 202 ]'), job["metadata"]["name"]
        assert spec["activeDeadlineSeconds"] > attempts * sleep, job["metadata"]["name"]
