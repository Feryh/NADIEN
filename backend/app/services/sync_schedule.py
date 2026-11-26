"""Expected sync schedules against the collector jobs that actually ran: which runs happened, failed or were missed.

The schedules themselves live outside the app (Kubernetes CronJobs in deploy/k8s/cronjobs.yaml, the worker's own
daily sync-all), so they are declared in NADIEN_SYNC_SCHEDULES, e.g.
  [{"key": "utilization", "label": "Utilisasi Observium", "kinds": ["observium-utilization"], "every_minutes": 10},
   {"key": "sync-all", "label": "Sync semua", "kinds": ["sync-all-scheduled"], "daily_at": "02:30",
    "grace_minutes": 180}]
Without it, only the in-process daily sync-all is expected (when the scheduler is on).

Every expected run is matched to a job of one of its kinds that started within [due - 2 min, due + grace]. A run
with no such job is missed; when another sync was running at that moment it is named, since the runner takes one
sync at a time and a CronJob's request is refused (409) while it is busy.
"""

from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from typing import Any, Literal
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models as m
from ..config import settings

EARLY = timedelta(minutes=2)  # clocks of the CronJob controller and the DB are not exactly in step
DAILY_RUNS = 7  # daily schedules: the last week
INTERVAL_WINDOW = timedelta(hours=24)  # interval schedules: the last day
MAX_PROBLEMS = 20

RunState = Literal["ok", "late", "failed", "running", "missed", "due"]


@dataclass(frozen=True)
class Schedule:
    key: str
    label: str
    kinds: tuple[str, ...]
    every_minutes: int | None = None
    daily_at: time | None = None
    grace_minutes: int | None = None
    source: str = "cronjob"

    @property
    def grace(self) -> timedelta:
        if self.grace_minutes is not None:
            return timedelta(minutes=self.grace_minutes)
        if self.every_minutes:
            return timedelta(minutes=max(self.every_minutes // 2, 1))
        return timedelta(minutes=30)

    @property
    def when(self) -> str:
        return f"tiap {self.every_minutes} menit" if self.every_minutes else f"tiap hari {self.daily_at:%H:%M}"


class ScheduleError(ValueError):
    pass


def _parse(spec: dict[str, Any]) -> Schedule:
    try:
        kinds = tuple(spec["kinds"])
        every = spec.get("every_minutes")
        daily = spec.get("daily_at")
        if bool(every) == bool(daily) or not kinds:
            raise ScheduleError(f"Jadwal {spec.get('key')}: isi kinds dan tepat satu dari every_minutes / daily_at")
        hours, minutes = (int(part) for part in str(daily).split(":")) if daily else (0, 0)
        return Schedule(key=str(spec["key"]), label=str(spec.get("label") or spec["key"]), kinds=kinds,
                        every_minutes=int(every) if every else None, daily_at=time(hours, minutes) if daily else None,
                        grace_minutes=spec.get("grace_minutes"), source=str(spec.get("source") or "cronjob"))
    except (KeyError, TypeError, ValueError) as exc:
        if isinstance(exc, ScheduleError):
            raise
        raise ScheduleError(f"Jadwal sync tidak valid: {spec}") from exc


def configured() -> list[Schedule]:
    if settings.sync_schedules is not None:
        return [_parse(spec) for spec in settings.sync_schedules]
    at = (settings.gitweb_daily_at or "").strip()
    if not (settings.scheduler_enabled and at):
        return []
    # The in-process scheduler retries every minute while another sync holds the runner.
    return [_parse({"key": "sync-all", "label": "Sync semua (terjadwal)", "kinds": ["sync-all-scheduled"],
                    "daily_at": at, "grace_minutes": 180, "source": "worker"})]


def _due_times(schedule: Schedule, now: datetime, tz: ZoneInfo) -> list[datetime]:
    """Expected start times, oldest first, that are already due."""
    local_now = now.astimezone(tz)
    if schedule.every_minutes:
        step = timedelta(minutes=schedule.every_minutes)
        midnight = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
        latest = midnight + step * ((local_now - midnight) // step)
        count = int(INTERVAL_WINDOW / step)
        return [latest - step * i for i in range(count - 1, -1, -1)]
    assert schedule.daily_at is not None
    today = datetime.combine(local_now.date(), schedule.daily_at, tzinfo=tz)
    latest = today if today <= local_now else today - timedelta(days=1)
    return [latest - timedelta(days=i) for i in range(DAILY_RUNS - 1, -1, -1)]


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _job_view(job: m.CollectorJob | None) -> dict[str, Any] | None:
    if job is None:
        return None
    started, finished = _aware(job.started_at), _aware(job.finished_at)
    return {"id": job.id, "kind": job.kind, "status": job.status, "started_at": started.isoformat() if started else None,
            "finished_at": finished.isoformat() if finished else None,
            "duration_s": round((finished - started).total_seconds()) if finished and started else None,
            "error": (job.error or "")[:200] or None}


def _blocking(jobs: list[m.CollectorJob], due: datetime, now: datetime) -> m.CollectorJob | None:
    """The sync that held the runner at the due time, if any."""
    for job in jobs:
        started, finished = _aware(job.started_at), _aware(job.finished_at) or now
        if started and started <= due <= finished:
            return job
    return None


def _first_job(schedule: Schedule, jobs: list[m.CollectorJob], start: datetime, end: datetime) -> m.CollectorJob | None:
    return next((job for job in jobs if job.kind in schedule.kinds and start <= _aware(job.started_at) <= end), None)


def _run(schedule: Schedule, due: datetime, next_due: datetime, jobs: list[m.CollectorJob],
         now: datetime) -> dict[str, Any]:
    start, end = due - EARLY, due + schedule.grace
    match = _first_job(schedule, jobs, start, end)
    late = match is None and now > end
    if late:
        # Ran after its window but before the next one was due, e.g. the worker catching up a run missed while
        # the API was down: it did run, so it is not missed.
        match = _first_job(schedule, jobs, end, min(next_due - EARLY, now))
    state: RunState
    if match is not None:
        state = {"ok": "late" if late else "ok", "failed": "failed"}.get(match.status, "running")
    else:
        state = "due" if now <= end else "missed"
    run: dict[str, Any] = {"due_at": due.isoformat(), "state": state, "job": _job_view(match)}
    if state == "missed" and (blocker := _blocking(jobs, due, now)) is not None:
        run["blocked_by"] = _job_view(blocker)
    return run


def status(db: Session, now: datetime | None = None) -> dict[str, Any]:
    """Per schedule: the latest expected run, counts over the window and the runs that failed or were missed."""
    now = now or datetime.now(timezone.utc)
    tz = ZoneInfo(settings.schedule_timezone)
    try:
        schedules = configured()
    except ScheduleError as exc:
        return {"timezone": settings.schedule_timezone, "schedules": [], "problems": 0, "error": str(exc)}
    since = now - max(timedelta(days=DAILY_RUNS + 1), INTERVAL_WINDOW + timedelta(hours=1))
    jobs = list(db.scalars(select(m.CollectorJob).where(m.CollectorJob.started_at >= since)
                           .order_by(m.CollectorJob.started_at)))
    rows = []
    for schedule in schedules:
        dues = _due_times(schedule, now, tz)
        period = timedelta(minutes=schedule.every_minutes) if schedule.every_minutes else timedelta(days=1)
        runs = [_run(schedule, due, nxt, jobs, now) for due, nxt in zip(dues, [*dues[1:], dues[-1] + period])]
        settled = [run for run in runs if run["state"] != "due"]
        counts = {state: sum(run["state"] == state for run in runs)
                  for state in ("ok", "late", "failed", "missed", "running")}
        last_ok = next((run["job"] for run in reversed(runs) if run["state"] in ("ok", "late")), None)
        rows.append({
            "key": schedule.key, "label": schedule.label, "kinds": list(schedule.kinds), "when": schedule.when,
            "source": schedule.source, "grace_minutes": int(schedule.grace.total_seconds() // 60),
            "last": settled[-1] if settled else None, "next_due": runs[-1] if runs and runs[-1]["state"] == "due" else None,
            "expected": len(settled), "counts": counts, "last_ok": last_ok,
            "problems": [run for run in reversed(runs) if run["state"] in ("failed", "missed", "late")][:MAX_PROBLEMS],
        })
    problems = sum(1 for row in rows if row["last"] and row["last"]["state"] in ("failed", "missed"))
    return {"timezone": settings.schedule_timezone, "schedules": rows, "problems": problems, "error": None}


def health_check(summary: dict[str, Any]) -> dict[str, Any]:
    """Data-health row: schedules whose latest expected run failed or never started."""
    bad = [row["label"] for row in summary["schedules"] if row["last"] and row["last"]["state"] in ("failed", "missed")]
    hint = (f"Run terakhir gagal/terlewat: {', '.join(bad)}. Lihat Collectors → Jadwal sync." if bad
            else "Run terakhir setiap jadwal sync sudah jalan (yang terlambat terlihat di Collectors → Jadwal sync).")
    if summary.get("error"):
        bad, hint = ["config"], summary["error"]
    return {"key": "sync_schedule", "label": "Jadwal sync terlewat / gagal", "count": len(bad),
            "total": len(summary["schedules"]), "severity": "warn" if bad else "ok", "hint": hint}


def job_history(db: Session, limit: int = 100, kind: str | None = None) -> list[dict[str, Any]]:
    query = select(m.CollectorJob).order_by(m.CollectorJob.started_at.desc()).limit(limit)
    if kind:
        query = query.where(m.CollectorJob.kind == kind)
    out = []
    for job in db.scalars(query):
        view = _job_view(job) or {}
        report = job.report if isinstance(job.report, dict) else {}
        view["phase"] = job.phase
        view["counts"] = report.get("counts") if isinstance(report.get("counts"), dict) else None
        steps = report.get("steps")
        if isinstance(steps, list):
            view["steps"] = [{"step": s.get("step"), "status": s.get("status"), "duration_s": s.get("duration_s")}
                             for s in steps if isinstance(s, dict)]
        out.append(view)
    return out
