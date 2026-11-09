"""Run long collector syncs in a background thread, one at a time across every API process.

A full Observium sync of a large network takes many minutes — longer than any browser or proxy
will hold an HTTP request — so the API starts the job and returns immediately; the UI polls
`/api/collectors/jobs/current`. Job state lives in the `collector_jobs` table so that several
uvicorn workers or Kubernetes replicas agree on what is running (the thread itself runs in the
process that accepted the request and refreshes a heartbeat; a job whose heartbeat stops is
reported as lost). The last finished job is also written to disk as before.
"""

import json
import threading
import uuid
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from .. import models as m
from ..backup import backup_dir
from ..database import SessionLocal
from ..services import cache

Progress = Callable[[str], None]
JobFn = Callable[[Session, Progress], dict]

HEARTBEAT_S = 30
STALE_AFTER = timedelta(seconds=HEARTBEAT_S * 4)


class JobBusy(RuntimeError):
    def __init__(self, job: dict[str, Any]):
        super().__init__(f"A {job['kind']} sync is already running (phase: {job['phase']})")
        self.job = job


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime | None) -> datetime | None:
    # SQLite returns naive datetimes even for timezone=True columns.
    return value.replace(tzinfo=timezone.utc) if value is not None and value.tzinfo is None else value


def _as_dict(row: m.CollectorJob) -> dict[str, Any]:
    return {"id": row.id, "kind": row.kind, "status": row.status, "phase": row.phase,
            "started_at": _aware(row.started_at).isoformat(),
            "finished_at": _aware(row.finished_at).isoformat() if row.finished_at else None,
            "report": row.report, "error": row.error}


def _jsonable(report: Any) -> Any:
    return json.loads(json.dumps(report, default=str))


class JobRunner:
    def __init__(self, session_factory: sessionmaker = SessionLocal, state_file: Path | None = None):
        self._session_factory = session_factory
        self._state_file = state_file
        self._lock = threading.Lock()
        self._local: dict[str, Any] | None = None  # the job this process is running (or ran last)
        self._thread: threading.Thread | None = None

    @property
    def state_file(self) -> Path:
        return self._state_file or backup_dir(self._session_factory.kw["bind"]) / "last-sync-job.json"

    # ------------------------------------------------------------------ start
    def start(self, kind: str, fn: JobFn) -> dict[str, Any]:
        with self._lock, self._session_factory() as db:
            busy = self._running_row(db)
            if busy is not None:
                raise JobBusy(_as_dict(busy))
            now = _now()
            row = m.CollectorJob(id=uuid.uuid4().hex[:12], kind=kind, status="running", phase="starting",
                                 started_at=now, heartbeat_at=now)
            db.add(row)
            db.commit()
            # Another process may have inserted at the same moment: the earliest running job wins.
            first = self._running_row(db)
            if first is not None and first.id != row.id:
                row.status, row.phase, row.error, row.finished_at = "failed", "done", "superseded", _now()
                db.commit()
                raise JobBusy(_as_dict(first))
            job = _as_dict(row)
        self._local = job
        self._thread = threading.Thread(target=self._run, args=(job, fn), name=f"sync-{kind}", daemon=True)
        self._thread.start()
        return dict(job)

    def _run(self, job: dict[str, Any], fn: JobFn) -> None:
        stop = threading.Event()
        beat = threading.Thread(target=self._heartbeat, args=(job, stop), daemon=True)
        beat.start()
        db = self._session_factory()
        try:
            report = fn(db, lambda phase: self._set_phase(job, phase))
            job["report"] = report
            # A multi-step job (sync-all) finishes and keeps its report even when one step failed.
            if isinstance(report, dict) and report.get("status") == "failed":
                job["status"] = "failed"
                job["error"] = report.get("error") or f"failed steps: {', '.join(report.get('failed_steps') or [])}"
            else:
                job["status"] = "ok"
        except Exception as exc:  # noqa: BLE001 — any failure must end up in the job, not kill the thread silently
            db.rollback()
            job["status"] = "failed"
            job["error"] = f"{type(exc).__name__}: {exc}"
        finally:
            db.close()
            stop.set()
            job["phase"] = "done"
            job["finished_at"] = _now().isoformat()
            self._persist(job, final=True)
            self._save(job)
            cache.clear()  # the sync changed what every read model shows

    # ------------------------------------------------------------------ persistence
    def _set_phase(self, job: dict[str, Any], phase: str) -> None:
        if job.get("phase") != phase:
            job["phase"] = phase
            self._persist(job)

    def _heartbeat(self, job: dict[str, Any], stop: threading.Event) -> None:
        while not stop.wait(HEARTBEAT_S):
            self._persist(job)

    def _persist(self, job: dict[str, Any], final: bool = False) -> None:
        try:
            with self._session_factory() as db:
                row = db.get(m.CollectorJob, job["id"])
                if row is None:
                    return
                row.phase = job["phase"][:64]
                row.heartbeat_at = _now()
                if final:
                    row.status = job["status"]
                    row.finished_at = _now()
                    row.report = _jsonable(job.get("report"))
                    row.error = job.get("error")
                db.commit()
        except SQLAlchemyError:
            pass  # a busy database must not kill the sync; the next heartbeat retries

    def _save(self, job: dict[str, Any]) -> None:
        try:
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            self.state_file.write_text(json.dumps(job, default=str, indent=2), encoding="utf-8")
        except OSError:
            pass  # the database copy is still served

    def _running_row(self, db: Session) -> m.CollectorJob | None:
        rows = db.scalars(select(m.CollectorJob).where(m.CollectorJob.status == "running")
                          .order_by(m.CollectorJob.started_at, m.CollectorJob.id)).all()
        for row in rows:
            local = self._local if self._local and self._local["id"] == row.id else None
            if local is None and _now() - _aware(row.heartbeat_at) > STALE_AFTER:
                row.status, row.phase, row.finished_at = "failed", "done", _now()
                row.error = "lost: the process running this sync stopped (restart or crash)"
                db.commit()
                continue
            return row
        return None

    # ------------------------------------------------------------------ queries
    def running(self) -> dict[str, Any] | None:
        if self._local and self._local["status"] == "running":
            return dict(self._local)
        try:
            with self._session_factory() as db:
                row = self._running_row(db)
                return _as_dict(row) if row else None
        except SQLAlchemyError:
            return None

    def current(self) -> dict[str, Any] | None:
        if self._local and self._local["status"] == "running":
            return dict(self._local)
        try:
            with self._session_factory() as db:
                self._running_row(db)  # expire lost jobs first
                row = db.scalars(select(m.CollectorJob).order_by(m.CollectorJob.started_at.desc()).limit(1)).first()
                if row is not None:
                    return _as_dict(row)
        except SQLAlchemyError:
            pass
        try:
            return json.loads(self.state_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def wait(self, timeout: float | None = None) -> dict[str, Any] | None:
        """Block until the running job finishes (used by tests)."""
        if self._thread is not None:
            self._thread.join(timeout)
        return dict(self._local) if self._local else self.current()


runner = JobRunner()


def ensure_idle(job_runner: JobRunner) -> None:
    """Refuse bulk writes (discovery, config import, backup) while a sync job is writing.

    SQLite allows one writer at a time; a second large transaction would make one of them
    time out with "database is locked" and abort a multi-minute sync.
    """
    from fastapi import HTTPException

    job = job_runner.running()
    if job:
        raise HTTPException(409, f"Sync {job['kind']} sedang berjalan (tahap: {job['phase']}); coba lagi setelah selesai")
