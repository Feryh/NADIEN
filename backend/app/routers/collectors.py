from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models as m
from ..backup import BackupError, backup_dir, create_backup, is_backupable, list_backups
from ..collectors import jobs
from ..collectors.configs import parse_config
from ..collectors.configs.oxidized import OxidizedClient, os_hint
from ..collectors.configs.rancid import RancidError, open_from_settings
from ..collectors.observium.client import ObserviumClient, ObserviumError
from ..collectors.runner import JobBusy, ensure_idle, runner
from ..collectors.observium.sync import ALL_PARTS, SOURCE_NAME
from ..config import settings
from ..database import get_db
from ..services.topology import norm_host

router = APIRouter(prefix="/collectors", tags=["Collectors"])

MAX_CONFIG_BYTES = 5_000_000


@router.get("/status")
def status(db: Session = Depends(get_db)):
    """Which collectors are configured (credentials are never echoed back)."""
    source = db.scalar(select(m.IntegrationSource).where(m.IntegrationSource.name == SOURCE_NAME))
    auth = "token" if settings.observium_token else ("basic" if settings.observium_user and settings.observium_password else None)
    return {
        "observium": {
            "url": settings.observium_url,
            "configured": auth is not None,
            "auth": auth,
            "last_sync_at": source.last_sync_at if source else None,
            "last_status": source.last_status if source else None,
        },
        "rancid": {
            "source": settings.rancid_path
            or (f"sftp://{settings.rancid_ssh_host}{settings.rancid_remote_path}" if settings.rancid_ssh_host else None),
            "configured": bool(settings.rancid_path or (settings.rancid_ssh_host and settings.rancid_ssh_user)),
        },
        "oxidized": {"url": settings.oxidized_url, "configured": bool(settings.oxidized_url)},
        "parts": sorted(ALL_PARTS),
    }


def _start(kind: str, fn) -> dict:
    try:
        return runner.start(kind, fn)
    except JobBusy as exc:
        raise HTTPException(409, str(exc)) from exc


@router.get("/jobs/current")
def current_job():
    """The running sync (status, phase) or the last finished one with its full report."""
    return runner.current() or {"status": "idle"}


@router.post("/observium/sync", status_code=202)
def observium_sync(
    parts: list[str] = Query(default=sorted(ALL_PARTS)),
    discover: bool = True,
):
    """Start a background job: back up the DB, then pull devices/ports/addresses (inventory),
    LLDP (neighbours) and port rates (utilization). Poll /collectors/jobs/current for the result."""
    unknown = set(parts) - ALL_PARTS
    if unknown:
        raise HTTPException(422, f"Unknown parts: {', '.join(sorted(unknown))}")
    try:
        client = ObserviumClient()  # credentials checked now, not minutes later in the job
    except ObserviumError as exc:
        raise HTTPException(502, str(exc)) from exc
    selected = set(parts)

    def job(db: Session, progress) -> dict:
        try:
            return jobs.run_observium(db, selected, discover, client=client, progress=progress)
        finally:
            client.close()

    kind = "observium-utilization" if selected == {"utilization"} else "observium"
    return _start(kind, job)


async def _read_text(request: Request) -> str:
    body = await request.body()
    if len(body) > MAX_CONFIG_BYTES:
        raise HTTPException(413, "Config too large")
    if not body.strip():
        raise HTTPException(422, "Empty config body")
    return body.decode("utf-8", errors="replace")


@router.post("/configs/parse")
async def parse_preview(request: Request, vendor: str | None = None):
    """Parse a config (raw text body) and return what would be imported — nothing is written."""
    text = await _read_text(request)
    try:
        return parse_config(text, vendor).as_dict()
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.post("/configs/rancid/sync", status_code=202)
def rancid_sync(discover: bool = True):
    """Start a background job: back up the DB, then read every config in Observium's RANCID archive."""
    try:
        opened = open_from_settings()  # configuration / SSH errors reported now
    except RancidError as exc:
        raise HTTPException(400 if "not configured" in str(exc) else 502, str(exc)) from exc
    return _start("rancid", lambda db, progress: jobs.run_rancid(db, discover, opened, progress))


@router.post("/configs/oxidized/sync", status_code=202)
def oxidized_sync(discover: bool = True):
    """Start a background job: back up the DB, then fetch every device config from Oxidized."""
    try:
        client = OxidizedClient()
    except RuntimeError as exc:
        raise HTTPException(400, str(exc)) from exc
    return _start("oxidized", lambda db, progress: jobs.run_oxidized(db, discover, client, progress))


@router.post("/configs/{hostname}")
async def import_config(
    hostname: str,
    request: Request,
    vendor: str | None = None,
    discover: bool = Query(True, description="Re-run topology discovery so config-described links appear"),
    db: Session = Depends(get_db),
):
    """Back up the DB, then merge one device's config (raw text body) into the inventory."""
    ensure_idle(runner)
    text = await _read_text(request)
    device = next((d for d in db.scalars(select(m.Device)) if norm_host(d.hostname) == norm_host(hostname)), None)
    if device is None:
        raise HTTPException(404, f"Device {hostname} not in inventory — run the Observium sync first")
    vendors = {v.id: v for v in db.scalars(select(m.Vendor))}
    try:
        cfg = parse_config(text, vendor or os_hint(device, vendors))
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    try:
        return jobs.run_config_import(db, device, cfg, discover)
    except jobs.JobError as exc:
        raise HTTPException(500, str(exc)) from exc


# --------------------------------------------------------------------------- backups

backups = APIRouter(prefix="/backups", tags=["Backups"])


@backups.get("")
def get_backups(db: Session = Depends(get_db)):
    engine = jobs._engine(db)
    return {
        "directory": str(backup_dir(engine)),
        "keep": settings.backup_keep,
        "enabled": settings.backup_before_sync,
        "supported": is_backupable(engine),
        "backups": [b.as_dict() for b in list_backups(engine)] if is_backupable(engine) else [],
    }


@backups.post("", status_code=201)
def create_manual_backup(db: Session = Depends(get_db)):
    """Take a backup now. Restore is CLI-only on purpose: `python -m app.backup restore <name>`."""
    ensure_idle(runner)  # every sync already starts with its own backup
    try:
        db.commit()
        return create_backup(jobs._engine(db), "manual").as_dict()
    except BackupError as exc:
        raise HTTPException(400, str(exc)) from exc
