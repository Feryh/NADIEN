from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models as m
from ..config import settings
from ..database import SessionLocal, get_db
from ..services import changes as svc
from ..services.auth import CurrentUser, audit, current_user, require
from ..services.configgen import Addressing
from ..services.device_cli import Jump, Target
from .configs import addressing_params

router = APIRouter(prefix="/changes", tags=["Change Execution"])


class ChangeIn(BaseModel):
    service_id: int
    title: str | None = Field(None, max_length=200)
    note: str | None = Field(None, max_length=4000)


class ReviewIn(BaseModel):
    note: str | None = Field(None, max_length=4000)


class RunIn(BaseModel):
    site: Literal["A", "B"]
    phase: svc.Phase
    protocol: Literal["ssh", "telnet"] = "ssh"
    port: int | None = Field(None, ge=1, le=65535)
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)
    use_jump: bool = True
    jump_password: str | None = Field(None, max_length=256)  # only when the jump host has its own account


def _run_dict(run: m.ChangeRun, with_log: bool = True) -> dict:
    out = {"id": run.id, "site": run.site, "phase": run.phase, "protocol": run.protocol, "status": run.status,
           "started_by": run.started_by, "device_user": run.device_user, "started_at": run.started_at,
           "finished_at": run.finished_at, "error": run.error}
    if with_log:
        out["log"] = run.log
    return out


def _change_dict(change: m.ChangeRequest, runs: list[m.ChangeRun] | None = None) -> dict:
    out = {"id": change.id, "title": change.title, "service_id": change.service_id, "params": change.params,
           "status": change.status, "note": change.note, "created_by": change.created_by,
           "approved_by": change.approved_by, "approved_at": change.approved_at, "review_note": change.review_note,
           "created_at": change.created_at, "updated_at": change.updated_at}
    if runs is not None:
        out["sites"] = change.sites
        out["runs"] = [_run_dict(r, with_log=False) for r in runs]
    return out


def _get(db: Session, change_id: int) -> m.ChangeRequest:
    change = db.get(m.ChangeRequest, change_id)
    if change is None:
        raise HTTPException(404, "Change not found")
    return change


def _runs(db: Session, change_id: int) -> list[m.ChangeRun]:
    return list(db.scalars(select(m.ChangeRun).where(m.ChangeRun.change_id == change_id).order_by(m.ChangeRun.id)))


@router.get("")
def list_changes(status: str | None = Query(None, max_length=24), db: Session = Depends(get_db)):
    stmt = select(m.ChangeRequest).order_by(m.ChangeRequest.id.desc()).limit(500)
    if status:
        stmt = stmt.where(m.ChangeRequest.status == status)
    return [_change_dict(c) for c in db.scalars(stmt)]


@router.post("", status_code=201)
def create_change(payload: ChangeIn, request: Request, addressing: Addressing = Depends(addressing_params),
                  db: Session = Depends(get_db), me: CurrentUser = Depends(current_user)):
    """Freezes the generator's output for this service and these parameters (same query as /configs)."""
    service = db.get(m.Service, payload.service_id)
    if service is None:
        raise HTTPException(404, "Service not found")
    sites = svc.build_sites(db, service, addressing)
    if not any(s["config"] for s in sites):
        raise HTTPException(422, "Tidak ada konfigurasi yang bisa dibuat untuk layanan ini.")
    change = m.ChangeRequest(title=payload.title or f"{service.circuit_id} — {service.service_type.upper()}",
                             service_id=service.id, params=dict(request.query_params), sites=sites,
                             note=payload.note, created_by=me.username, status="pending_approval")
    db.add(change)
    db.commit()
    return _change_dict(change, [])


@router.get("/{change_id}")
def get_change(change_id: int, db: Session = Depends(get_db)):
    change = _get(db, change_id)
    return _change_dict(change, _runs(db, change_id))


def _review(db: Session, change: m.ChangeRequest, me: CurrentUser, note: str | None, status: str) -> dict:
    if change.status != "pending_approval":
        raise HTTPException(409, f"Change berstatus {change.status}, bukan menunggu approval.")
    if me.username == change.created_by and settings.auth_enabled:
        raise HTTPException(403, "Approval harus oleh orang lain, bukan pembuat change.")
    change.status, change.review_note = status, note
    change.approved_by, change.approved_at = me.username, datetime.now(timezone.utc)
    audit(db, me.username, f"change.{'approve' if status == 'approved' else 'reject'}", f"change {change.id}", note)
    db.commit()
    return _change_dict(change, _runs(db, change.id))


@router.post("/{change_id}/approve")
def approve_change(change_id: int, payload: ReviewIn, db: Session = Depends(get_db),
                   me: CurrentUser = Depends(require("approver"))):
    return _review(db, _get(db, change_id), me, payload.note, "approved")


@router.post("/{change_id}/reject")
def reject_change(change_id: int, payload: ReviewIn, db: Session = Depends(get_db),
                  me: CurrentUser = Depends(require("approver"))):
    return _review(db, _get(db, change_id), me, payload.note, "rejected")


@router.post("/{change_id}/runs", status_code=202)
def start_run(change_id: int, payload: RunIn, db: Session = Depends(get_db),
              me: CurrentUser = Depends(require("engineer"))):
    """Starts one CLI session; follow it with GET …/runs/{run_id}. The password is never stored or logged."""
    if payload.protocol == "telnet" and not settings.device_telnet_enabled:
        raise HTTPException(422, "Telnet dinonaktifkan di server ini. Gunakan SSH.")
    change = _get(db, change_id)
    try:
        svc.check_can_run(change, _runs(db, change_id), payload.site, payload.phase)
    except svc.ChangeError as exc:
        raise HTTPException(409, str(exc)) from exc
    site = svc._site(change, payload.site)
    target = Target(site["host"], payload.protocol, payload.username, payload.password, payload.port, site["dialect"])
    jump = None
    if payload.use_jump and settings.device_jump_host:
        jump = Jump(settings.device_jump_host, settings.device_jump_port, settings.device_jump_user or payload.username,
                    payload.jump_password or payload.password)
    run = m.ChangeRun(change_id=change.id, site=payload.site, phase=payload.phase, protocol=payload.protocol,
                      status="running", started_by=me.username, device_user=payload.username, log="")
    db.add(run)
    if payload.phase in ("push", "rollback") and change.status == "approved":
        change.status = "in_progress"
    db.commit()
    known_hosts = svc.known_hosts_path(db)
    svc.start_background(lambda: svc.execute(SessionLocal, run.id, target, jump, known_hosts))
    return _run_dict(run)


@router.get("/{change_id}/runs/{run_id}")
def get_run(change_id: int, run_id: int, db: Session = Depends(get_db)):
    run = db.get(m.ChangeRun, run_id)
    if run is None or run.change_id != change_id:
        raise HTTPException(404, "Run not found")
    return _run_dict(run)
