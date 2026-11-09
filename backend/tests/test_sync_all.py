"""One "Sync semua" job: Observium main + CSR, gitweb configs, one topology discovery, RRD history main + CSR."""

import threading
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app import models as m
from app.backup import list_backups
from app.collectors import jobs, scheduler
from app.collectors.observium.client import ObserviumClient
from app.collectors.observium.rrd import rrd_window_days, sync_rrd
from app.collectors.observium.sync import sync_observium
from app.collectors.runner import JobRunner
from app.config import settings
from app.database import Base, get_db
from app.main import app

from .conftest import observium_transport
from .test_gitweb import FakeGitweb

ORDER = ["backup", "observium", "observium_csr", "configs", "discovery", "rrd", "rrd_csr"]


def _observium() -> ObserviumClient:
    return ObserviumClient(base_url="http://o.test", token="t", transport=observium_transport())


class FakeRrd:
    """An RRD source that remembers which links were asked for over which window."""

    name = "fake"
    chunk_size = 100

    def __init__(self):
        self.calls: list[tuple[list[int], int]] = []

    def usable(self, ref: m.ObserviumPort) -> bool:
        return True

    def fetch(self, chunk, days: int) -> dict:
        self.calls.append(([t.link.id for t in chunk], days))
        return {}

    def close(self) -> None:
        pass


def _sources(events: list[str], fail: str | None = None, rrd: FakeRrd | None = None) -> jobs.SyncAllSources:
    def made(name: str, factory):
        def build():
            events.append(name)
            if name == fail:
                raise RuntimeError(f"{name} unreachable")
            return factory()
        return build

    return jobs.SyncAllSources(
        observium=made("observium", _observium),
        observium_csr=made("observium_csr", _observium),
        gitweb=made("gitweb", lambda: FakeGitweb().client()),
        rrd=made("rrd", lambda: rrd or FakeRrd()),
        rrd_csr=made("rrd_csr", FakeRrd),
    )


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setattr(settings, "gitweb_url", "http://gitweb.test/gitweb/")
    monkeypatch.setattr(settings, "observium_csr_url", "http://csr.test")


@pytest.fixture
def file_db(tmp_path, monkeypatch):
    """A real SQLite file (backups need one) with backups going to tmp_path/backups."""
    monkeypatch.setattr(settings, "backup_dir", str(tmp_path / "backups"))
    monkeypatch.setattr(settings, "backup_before_sync", True)
    engine = create_engine(f"sqlite:///{tmp_path / 'nadien.db'}")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)()
    yield session
    session.close()
    engine.dispose()


def _steps(report: dict) -> dict[str, dict]:
    return {s["step"]: s for s in report["steps"]}


def test_should_run_every_step_in_order_when_sync_all_starts(db, configured):
    events: list[str] = []
    report = jobs.run_all(db, sources=_sources(events))
    assert [s["step"] for s in report["steps"]] == ORDER
    assert report["status"] == "ok" and all(s["status"] == "ok" for s in report["steps"])
    assert events == ["observium", "observium_csr", "gitweb", "rrd", "rrd_csr"]


def test_should_take_one_backup_when_sync_all_runs(file_db, configured):
    report = jobs.run_all(file_db, sources=_sources([]))
    backups = list_backups(file_db.get_bind())
    assert [b.reason for b in backups] == ["sync-all"] and _steps(report)["backup"]["report"]["status"] == "ok"


def test_should_stop_when_backup_fails(file_db, configured, tmp_path, monkeypatch):
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("x")
    monkeypatch.setattr(settings, "backup_dir", str(blocker / "backups"))  # mkdir under a file fails
    events: list[str] = []
    report = jobs.run_all(file_db, sources=_sources(events))
    assert report["status"] == "failed" and [s["step"] for s in report["steps"]] == ["backup"]
    assert report["steps"][0]["status"] == "failed" and events == []


def test_should_continue_with_next_step_when_a_step_fails(db, configured):
    events: list[str] = []
    report = jobs.run_all(db, sources=_sources(events, fail="observium"))
    steps = _steps(report)
    assert steps["observium"]["status"] == "failed" and "observium unreachable" in steps["observium"]["error"]
    assert all(steps[name]["status"] == "ok" for name in ORDER if name != "observium")
    assert report["status"] == "failed" and report["failed_steps"] == ["observium"]


def test_should_skip_csr_steps_when_csr_observium_is_not_configured(db, configured, monkeypatch):
    monkeypatch.setattr(settings, "observium_csr_url", "")
    events: list[str] = []
    report = jobs.run_all(db, sources=_sources(events))
    steps = _steps(report)
    assert steps["observium_csr"]["status"] == "skipped" and steps["rrd_csr"]["status"] == "skipped"
    assert "observium_csr" not in events and "rrd_csr" not in events and report["status"] == "ok"


def test_should_skip_configs_when_gitweb_is_not_configured(db, configured, monkeypatch):
    monkeypatch.setattr(settings, "gitweb_url", "")
    events: list[str] = []
    report = jobs.run_all(db, sources=_sources(events))
    assert _steps(report)["configs"]["status"] == "skipped" and "gitweb" not in events


def test_should_run_discovery_once_after_configs_when_sync_all_runs(db, configured, monkeypatch):
    from app.collectors.configs import gitweb as gitweb_mod
    from app.collectors.observium import sync as sync_mod

    events: list[str] = []
    calls: list[set[int] | None] = []
    real = jobs.discover_links

    def recording(session, valid_iface_ids=None):
        events.append("discover")
        calls.append(valid_iface_ids)
        return real(session, valid_iface_ids=valid_iface_ids)

    for module in (jobs, sync_mod, gitweb_mod):
        monkeypatch.setattr(module, "discover_links", recording)
    jobs.run_all(db, sources=_sources(events))
    assert events == ["observium", "observium_csr", "gitweb", "discover", "rrd", "rrd_csr"]
    assert len(calls) == 1 and calls[0]  # main Observium's ports enable phantom-port cleanup


def test_should_discover_without_phantom_cleanup_when_main_observium_failed(db, configured, monkeypatch):
    calls: list[set[int] | None] = []
    real = jobs.discover_links
    monkeypatch.setattr(jobs, "discover_links",
                        lambda session, valid_iface_ids=None: (calls.append(valid_iface_ids), real(session))[1])
    jobs.run_all(db, sources=_sources([], fail="observium"))
    assert calls == [None]


def test_should_use_backfill_window_when_link_has_no_rrd_history():
    assert rrd_window_days(has_history=False, days=2, backfill_days=90) == 90
    assert rrd_window_days(has_history=True, days=2, backfill_days=90) == 2
    assert rrd_window_days(has_history=False, days=7, backfill_days=None) == 7


def test_should_fetch_routine_window_only_for_links_with_old_samples_when_backfill_is_on(db):
    sync_observium(db, _observium())
    links = list(db.scalars(select(m.Link).order_by(m.Link.id)))
    old = datetime.now(timezone.utc) - timedelta(days=30)
    db.add(m.UtilizationSample(link_id=links[0].id, ts=old, in_gbps=1, out_gbps=1))
    db.commit()
    source = FakeRrd()
    report = sync_rrd(db, source, days=2, backfill_days=90)
    windows = {link_id: days for ids, days in source.calls for link_id in ids}
    assert windows[links[0].id] == 2 and all(windows[l.id] == 90 for l in links[1:] if l.id in windows)
    assert report["links_backfilled"] == len(windows) - 1


def test_should_mark_job_failed_with_report_when_sync_all_has_a_failed_step(job_runner):
    job_runner.start("sync-all", lambda _db, _p: {"status": "failed", "failed_steps": ["rrd"], "steps": []})
    job = job_runner.wait(5)
    assert job["status"] == "failed" and job["report"]["failed_steps"] == ["rrd"] and "rrd" in job["error"]


@pytest.fixture
def api(db):
    app.dependency_overrides[get_db] = lambda: db
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


def test_should_start_sync_all_job_when_api_called(api, job_runner, monkeypatch):
    seen: dict = {}
    monkeypatch.setattr(jobs, "run_all", lambda db, **kwargs: seen.update(kwargs) or {"status": "ok", "steps": []})
    resp = api.post("/api/collectors/all/sync", json={"rrd_days": 3, "gitweb_full": True})
    job = job_runner.wait(5)
    assert resp.status_code == 202 and resp.json()["kind"] == "sync-all"
    assert job["status"] == "ok" and seen["rrd_days"] == 3 and seen["gitweb_full"] is True


def test_should_start_sync_all_with_defaults_when_api_called_without_body(api, job_runner, monkeypatch):
    seen: dict = {}
    monkeypatch.setattr(jobs, "run_all", lambda db, **kwargs: seen.update(kwargs) or {"status": "ok", "steps": []})
    assert api.post("/api/collectors/all/sync").status_code == 202
    job_runner.wait(5)
    assert seen["rrd_days"] == 2 and seen["rrd_backfill_days"] == 90 and seen["gitweb_full"] is False


def test_should_refuse_sync_all_when_another_sync_is_running(api, job_runner):
    gate = threading.Event()
    job_runner.start("observium", lambda _db, _p: (gate.wait(5), {})[1])
    try:
        resp = api.post("/api/collectors/all/sync")
    finally:
        gate.set()
        job_runner.wait(5)
    assert resp.status_code == 409


def test_should_reject_sync_all_when_rrd_days_is_out_of_range(api, job_runner):
    assert api.post("/api/collectors/all/sync", json={"rrd_days": 0}).status_code == 422


# --------------------------------------------------------------------------- daily scheduler


@pytest.fixture
def sched(db, tmp_path, monkeypatch):
    from app.services import collector_settings

    monkeypatch.setattr(settings, "gitweb_url", "")  # the daily run no longer depends on gitweb
    monkeypatch.setattr(settings, "gitweb_daily_at", "02:30")
    monkeypatch.setattr(collector_settings, "refresh", lambda: None)
    factory = sessionmaker(bind=db.get_bind(), autoflush=False, expire_on_commit=False)
    return factory, JobRunner(session_factory=factory, state_file=tmp_path / "job.json")


def _at(hour: int, minute: int) -> datetime:
    return datetime(2026, 11, 3, hour, minute, tzinfo=timezone(timedelta(hours=7)))


def test_should_start_sync_all_when_daily_time_has_passed(sched, db, monkeypatch):
    factory, runner = sched
    calls: list[dict] = []
    monkeypatch.setattr(jobs, "run_all", lambda session, **kwargs: calls.append(kwargs) or {"status": "ok", "steps": []})
    job_id = scheduler.tick(runner, factory, now=_at(2, 31))
    runner.wait(5)
    assert job_id is not None and len(calls) == 1
    assert db.get(m.CollectorJob, job_id).kind == "sync-all-scheduled"
    assert [r.job for r in db.scalars(select(m.ScheduledRun))] == ["sync-all"]


def test_should_start_sync_all_once_per_day(sched):
    factory, runner = sched
    calls = []
    job = lambda db, progress: calls.append(1) or {}  # noqa: E731
    first = scheduler.tick(runner, factory, now=_at(2, 31), start_job=job)
    runner.wait(5)
    second = scheduler.tick(runner, factory, now=_at(9, 0), start_job=job)
    assert first is not None and second is None and calls == [1]


def test_should_not_start_sync_all_when_gitweb_daily_run_already_ran_today(sched, db):
    factory, runner = sched
    db.add(m.ScheduledRun(job="gitweb", run_date="2026-11-03"))
    db.commit()
    assert scheduler.tick(runner, factory, now=_at(9, 0), start_job=lambda db, p: {}) is None
    assert scheduler.tick(runner, factory, now=datetime(2026, 11, 4, 2, 31, tzinfo=timezone(timedelta(hours=7))),
                          start_job=lambda db, p: {}) is not None
    runner.wait(5)


def test_should_not_start_sync_all_when_daily_time_is_empty(sched, monkeypatch):
    factory, runner = sched
    monkeypatch.setattr(settings, "gitweb_daily_at", "")
    assert scheduler.tick(runner, factory, now=_at(9, 0), start_job=lambda db, p: {}) is None


# --------------------------------------------------------------------------- CLI


def test_should_exit_non_zero_when_a_sync_all_step_failed(monkeypatch, capsys):
    from app.collectors import run

    seen: dict = {}
    monkeypatch.setattr(run.collector_settings, "apply", lambda db: None)
    monkeypatch.setattr(jobs, "run_all",
                        lambda db, **kwargs: seen.update(kwargs) or {"status": "failed", "failed_steps": ["rrd"], "steps": []})
    code = run.main(["all", "--rrd-days", "3", "--rrd-backfill-days", "30", "--gitweb-full"])
    assert code == 1 and '"failed_steps"' in capsys.readouterr().out
    assert seen == {"rrd_days": 3, "rrd_backfill_days": 30, "gitweb_full": True}
