"""Run long collector syncs in a background thread, one at a time.

A full Observium sync of a large network takes many minutes — longer than any browser or proxy
will hold an HTTP request — so the API starts the job and returns immediately; the UI polls
`/api/collectors/jobs/current`. The last finished job is also written to disk so its report
survives an API restart.
"""

import json
import threading
import uuid
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session, sessionmaker

from ..backup import backup_dir
from ..database import SessionLocal

Progress = Callable[[str], None]
JobFn = Callable[[Session, Progress], dict]


class JobBusy(RuntimeError):
    def __init__(self, job: dict[str, Any]):
        super().__init__(f"A {job['kind']} sync is already running (phase: {job['phase']})")
        self.job = job


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class JobRunner:
    def __init__(self, session_factory: sessionmaker = SessionLocal, state_file: Path | None = None):
        self._session_factory = session_factory
        self._state_file = state_file
        self._lock = threading.Lock()
        self._current: dict[str, Any] | None = None
        self._thread: threading.Thread | None = None

    @property
    def state_file(self) -> Path:
        return self._state_file or backup_dir(self._session_factory.kw["bind"]) / "last-sync-job.json"

    def start(self, kind: str, fn: JobFn) -> dict[str, Any]:
        with self._lock:
            if self._current and self._current["status"] == "running":
                raise JobBusy(dict(self._current))
            job = {"id": uuid.uuid4().hex[:12], "kind": kind, "status": "running", "phase": "starting",
                   "started_at": _now(), "finished_at": None, "report": None, "error": None}
            self._current = job
            self._thread = threading.Thread(target=self._run, args=(job, fn), name=f"sync-{kind}", daemon=True)
            self._thread.start()
            return dict(job)

    def _run(self, job: dict[str, Any], fn: JobFn) -> None:
        db = self._session_factory()
        try:
            job["report"] = fn(db, lambda phase: job.__setitem__("phase", phase))
            job["status"] = "ok"
        except Exception as exc:  # noqa: BLE001 — any failure must end up in the job, not kill the thread silently
            db.rollback()
            job["status"] = "failed"
            job["error"] = f"{type(exc).__name__}: {exc}"
        finally:
            db.close()
            job["phase"] = "done"
            job["finished_at"] = _now()
            self._save(job)

    def _save(self, job: dict[str, Any]) -> None:
        try:
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            self.state_file.write_text(json.dumps(job, default=str, indent=2), encoding="utf-8")
        except OSError:
            pass  # the in-memory copy is still served

    def running(self) -> dict[str, Any] | None:
        job = self._current
        return dict(job) if job and job["status"] == "running" else None

    def current(self) -> dict[str, Any] | None:
        if self._current is not None:
            return dict(self._current)
        try:
            return json.loads(self.state_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def wait(self, timeout: float | None = None) -> dict[str, Any] | None:
        """Block until the running job finishes (used by tests)."""
        if self._thread is not None:
            self._thread.join(timeout)
        return self.current()


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
