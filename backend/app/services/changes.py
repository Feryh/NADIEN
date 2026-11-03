"""Change requests: generated scripts frozen for review, approved by someone else, then run on the devices.

Order per change: pre-check any time before implementation (show commands only); push site A, then site B,
each only once it succeeded; post-check after a site's push; rollback after a site's push was attempted.
One run at a time per change. Device passwords live only in the worker thread's memory.
"""

import threading
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models as m
from ..config import settings
from .configgen import Addressing, as_text, generate
from .device_cli import CliError, CliSession, CommandRejected, Jump, Stream, Target, open_stream
from .mop import site_checks

Phase = Literal["precheck", "push", "postcheck", "rollback"]
SITES = ("A", "B")
DEVICE_CHANGING = ("approved", "in_progress", "failed", "implemented", "rolled_back")
SCRIPT = {"precheck": "precheck", "push": "config", "postcheck": "postcheck", "rollback": "rollback"}  # phase -> site key


class ChangeError(Exception):
    """A request that conflicts with the change's state (HTTP 409)."""


def build_sites(db: Session, service: m.Service, addressing: Addressing) -> list[dict[str, Any]]:
    result = generate(db, service, addressing)
    checks = site_checks(db, service)
    sites = []
    for label in SITES:
        endpoint = result["endpoints"][label]
        device = db.get(m.Device, endpoint["device_id"]) if endpoint.get("device_id") else None
        sites.append({
            "label": label, "device_id": endpoint.get("device_id"), "hostname": endpoint["device"],
            "host": (device.mgmt_ip or device.hostname) if device else None,
            "dialect": endpoint["dialect"], "dialect_label": endpoint["dialect_label"],
            "precheck": checks[label]["precheck"],
            "config": as_text(result, label, "config").splitlines(),
            "rollback": as_text(result, label, "rollback").splitlines(),
            "postcheck": checks[label]["postcheck"],
            "warnings": endpoint["warnings"], "vc_ids": endpoint.get("vc_ids", []),
        })
    return sites


def _site(change: m.ChangeRequest, label: str) -> dict[str, Any]:
    return next(s for s in change.sites if s["label"] == label)


def _ok(runs: list[m.ChangeRun], site: str, phase: str) -> bool:
    return any(r.site == site and r.phase == phase and r.status == "ok" for r in runs)


def check_can_run(change: m.ChangeRequest, runs: list[m.ChangeRun], site: str, phase: Phase) -> None:
    if any(r.status == "running" for r in runs):
        raise ChangeError("Masih ada eksekusi yang berjalan untuk change ini; tunggu sampai selesai.")
    target = _site(change, site)
    if not target["host"]:
        raise ChangeError(f"Site {site} tidak punya perangkat/IP manajemen.")
    if not target[SCRIPT[phase]]:
        raise ChangeError(f"Tidak ada perintah {phase} untuk site {site}.")
    if phase == "precheck":
        if change.status not in ("pending_approval", "approved", "in_progress"):
            raise ChangeError(f"Pre-check tidak bisa dijalankan pada status {change.status}.")
        return
    if phase in ("push", "rollback") and change.status not in DEVICE_CHANGING:
        raise ChangeError("Change belum di-approve.")
    if phase == "push":
        if change.status in ("failed", "implemented", "rolled_back"):
            raise ChangeError(f"Push tidak bisa dijalankan pada status {change.status}; buat change baru.")
        if _ok(runs, site, "push"):
            raise ChangeError(f"Site {site} sudah berhasil di-push.")
        if site == "B" and _site(change, "A")["config"] and not _ok(runs, "A", "push"):
            raise ChangeError("Push site A dulu dan pastikan berhasil sebelum site B.")
    elif phase == "postcheck":
        if not _ok(runs, site, "push"):
            raise ChangeError(f"Post-check site {site} setelah push site itu berhasil.")
    elif phase == "rollback":
        if not any(r.site == site and r.phase == "push" for r in runs):
            raise ChangeError(f"Site {site} belum pernah di-push; tidak ada yang perlu di-rollback.")


def _status_after(change: m.ChangeRequest, runs: list[m.ChangeRun], run: m.ChangeRun) -> str:
    if run.phase == "rollback" and run.status == "ok":
        return "rolled_back"
    if run.phase != "push":
        return change.status
    if run.status != "ok":
        return "failed"
    pushable = [s["label"] for s in change.sites if s["config"]]
    return "implemented" if all(_ok(runs, label, "push") for label in pushable) else "in_progress"


def known_hosts_path(db: Session) -> Path:
    if settings.device_known_hosts:
        return Path(settings.device_known_hosts)
    from ..backup import backup_dir  # late: backup imports the engine

    return backup_dir(db.get_bind()) / "device_known_hosts"


Connector = Callable[[Target, Path, float, Jump | None], Stream]


def execute(session_factory: Callable[[], Session], run_id: int, target: Target, jump: Jump | None,
            known_hosts: Path, connect: Connector | None = None) -> None:
    """Runs in a worker thread; writes the transcript after every command so the page can follow it live."""
    connect = connect or open_stream
    db = session_factory()
    try:
        run = db.get(m.ChangeRun, run_id)
        change = db.get(m.ChangeRequest, run.change_id)
        commands: list[str] = _site(change, run.site)[SCRIPT[run.phase]]
        transcript: list[str] = []

        def flush() -> None:
            run.log = "".join(transcript)
            db.commit()

        def note(text: str) -> None:
            transcript.append(f"\n### {text}\n")
            flush()

        check = run.phase in ("push", "rollback")
        try:
            note(f"{run.phase} site {run.site} {target.host} via {target.protocol} oleh {run.started_by}")
            session = CliSession(connect(target, known_hosts, settings.device_timeout_s, jump), target,
                                 transcript.append, settings.device_timeout_s)
            try:
                session.login()
                flush()
                for command in commands:
                    session.run(command, check=check)
                    flush()
            finally:
                session.close()
            run.status = "ok"
            note("selesai")
        except CommandRejected as exc:
            run.status, run.error = "failed", f"{exc} — {exc.reply.strip()[:300]}"
            note(f"BERHENTI: {exc}")
        except CliError as exc:
            run.status, run.error = "failed", str(exc)
            note(f"GAGAL: {exc}")
        run.finished_at = datetime.now(timezone.utc)
        runs = list(db.scalars(select(m.ChangeRun).where(m.ChangeRun.change_id == change.id)))
        change.status = _status_after(change, runs, run)
        db.add(m.AuditLog(at=run.finished_at, username=run.started_by, action=f"change.{run.phase}",
                          target=f"change {change.id} site {run.site}", detail=f"{run.status}: {run.error or ''}"[:2000]))
        db.commit()
    except Exception as exc:  # never leave a run stuck in "running"
        db.rollback()
        run = db.get(m.ChangeRun, run_id)
        if run is not None and run.status == "running":
            run.status, run.error, run.finished_at = "failed", f"Kesalahan internal: {exc}", datetime.now(timezone.utc)
            db.commit()
        raise
    finally:
        db.close()


def start_background(work: Callable[[], None]) -> None:
    threading.Thread(target=work, daemon=True, name="change-run").start()
