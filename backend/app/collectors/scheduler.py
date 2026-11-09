"""Daily in-process scheduler: the sync-all job (Observium main + CSR, gitweb configs, LLDP/config topology,
RRD history) at NADIEN_GITWEB_DAILY_AT (schedule time zone; the setting kept its name from the gitweb-only run).

Every API process runs this loop; each day's run is claimed with a unique (job, date) row, so with
several uvicorn workers or replicas it still starts exactly once. A run that is due but blocked by
another sync is retried every minute; one missed while the API was down runs when it comes back.
Where an external scheduler is preferred (Kubernetes CronJob), set NADIEN_SCHEDULER_ENABLED=false.
"""

import logging
import threading
from collections.abc import Callable
from datetime import datetime, time
from zoneinfo import ZoneInfo

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import sessionmaker

from .. import models as m
from ..config import settings
from ..database import SessionLocal
from . import jobs
from .runner import JobBusy, JobRunner

log = logging.getLogger("nadien.scheduler")
CHECK_EVERY_S = 60
SYNC_ALL_JOB = "sync-all"
# Claimed by the gitweb-only daily run of earlier versions. A day it already ran counts as done, so a deploy after
# 02:30 does not start a full Observium + RRD sync in the middle of the working day.
LEGACY_JOBS = ("gitweb",)


def parse_hhmm(value: str) -> time:
    hours, minutes = value.strip().split(":")
    return time(int(hours), int(minutes))


def _claim(session_factory: sessionmaker, job: str, run_date: str) -> bool:
    try:
        with session_factory() as db:
            db.add(m.ScheduledRun(job=job, run_date=run_date))
            db.commit()
            return True
    except IntegrityError:
        return False  # another process already took today's run


def _ran_before_upgrade(session_factory: sessionmaker, run_date: str) -> bool:
    with session_factory() as db:
        return db.scalar(select(m.ScheduledRun.id).where(m.ScheduledRun.job.in_(LEGACY_JOBS),
                                                         m.ScheduledRun.run_date == run_date).limit(1)) is not None


def _release(session_factory: sessionmaker, job: str, run_date: str) -> None:
    with session_factory() as db:
        db.execute(delete(m.ScheduledRun).where(m.ScheduledRun.job == job, m.ScheduledRun.run_date == run_date))
        db.commit()


def _record(session_factory: sessionmaker, job: str, run_date: str, job_id: str) -> None:
    with session_factory() as db:
        row = db.query(m.ScheduledRun).filter_by(job=job, run_date=run_date).one_or_none()
        if row is not None:
            row.job_id = job_id
            db.commit()


def tick(runner: JobRunner, session_factory: sessionmaker = SessionLocal, now: datetime | None = None,
         start_job: Callable | None = None) -> str | None:
    """One scheduler check. Returns the started job id, or None when nothing was due/possible."""
    if start_job is None:  # real runs: pick up schedule/URL changes saved from the UI by any process
        from ..services import collector_settings

        collector_settings.refresh()
    at = (settings.gitweb_daily_at or "").strip()
    if not at:  # every source is optional inside sync-all, so only the time decides
        return None
    local = now or datetime.now(ZoneInfo(settings.schedule_timezone))
    if local.time() < parse_hhmm(at) or runner.running():
        return None
    run_date = local.date().isoformat()
    if _ran_before_upgrade(session_factory, run_date) or not _claim(session_factory, SYNC_ALL_JOB, run_date):
        return None
    job_fn = start_job or (lambda db, progress: jobs.run_all(db, progress=progress))
    try:
        job = runner.start("sync-all-scheduled", job_fn)
    except JobBusy:
        _release(session_factory, SYNC_ALL_JOB, run_date)  # retry next minute
        return None
    _record(session_factory, SYNC_ALL_JOB, run_date, job["id"])
    log.info("scheduled sync-all started (job %s)", job["id"])
    return job["id"]


class Scheduler:
    def __init__(self, runner: JobRunner):
        self._runner = runner
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._loop, name="nadien-scheduler", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.wait(CHECK_EVERY_S):
            try:
                tick(self._runner)
            except (SQLAlchemyError, ValueError) as exc:  # a bad setting or busy DB must not kill the loop
                log.warning("scheduler check failed: %s", exc)
