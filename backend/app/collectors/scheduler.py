"""Daily in-process scheduler: the gitweb RANCID update at NADIEN_GITWEB_DAILY_AT (schedule time zone).

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

from sqlalchemy import delete
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import sessionmaker

from .. import models as m
from ..config import settings
from ..database import SessionLocal
from . import jobs
from .runner import JobBusy, JobRunner

log = logging.getLogger("nadien.scheduler")
CHECK_EVERY_S = 60
GITWEB_JOB = "gitweb"


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
    at = (settings.gitweb_daily_at or "").strip()
    if not at or not settings.gitweb_url:
        return None
    local = now or datetime.now(ZoneInfo(settings.schedule_timezone))
    if local.time() < parse_hhmm(at) or runner.running():
        return None
    run_date = local.date().isoformat()
    if not _claim(session_factory, GITWEB_JOB, run_date):
        return None
    job_fn = start_job or (lambda db, progress: jobs.run_gitweb(db, progress=progress))
    try:
        job = runner.start("gitweb-scheduled", job_fn)
    except JobBusy:
        _release(session_factory, GITWEB_JOB, run_date)  # retry next minute
        return None
    _record(session_factory, GITWEB_JOB, run_date, job["id"])
    log.info("scheduled gitweb update started (job %s)", job["id"])
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
