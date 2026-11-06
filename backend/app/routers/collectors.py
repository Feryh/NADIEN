from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import models as m
from ..backup import BackupError, backup_dir, create_backup, is_backupable, list_backups
from ..collectors import jobs
from ..collectors.configs import parse_config
from ..collectors.configs.gitweb import GitwebClient, GitwebError
from ..collectors.configs.oxidized import OxidizedClient, os_hint
from ..collectors.configs.rancid import RancidError, open_from_settings
from ..collectors.observium.client import ObserviumClient, ObserviumError
from ..collectors.observium.rrd import RrdError
from ..collectors.observium.rrd import open_source as open_rrd_source
from ..collectors.runner import JobBusy, ensure_idle, runner
from ..collectors.observium.sync import ALL_PARTS, CSR_SOURCE_NAME, SOURCE_NAME
from ..config import settings
from ..database import get_db
from ..services import collector_settings
from ..services.topology import norm_host

def _current_settings() -> None:
    # Values saved in Pengaturan Collector may have been changed by another worker/replica.
    collector_settings.refresh()


router = APIRouter(prefix="/collectors", tags=["Collectors"], dependencies=[Depends(_current_settings)])

MAX_CONFIG_BYTES = 5_000_000


@router.get("/status")
def status(db: Session = Depends(get_db)):
    """Which collectors are configured (credentials are never echoed back)."""
    source = db.scalar(select(m.IntegrationSource).where(m.IntegrationSource.name == SOURCE_NAME))
    csr_source = db.scalar(select(m.IntegrationSource).where(m.IntegrationSource.name == CSR_SOURCE_NAME))
    auth = "token" if settings.observium_token else ("basic" if settings.observium_user and settings.observium_password else None)
    return {
        "observium": {
            "url": settings.observium_url,
            "configured": auth is not None,
            "auth": auth,
            "last_sync_at": source.last_sync_at if source else None,
            "last_status": source.last_status if source else None,
        },
        "observium_csr": {
            "url": settings.observium_csr_url,
            "configured": auth is not None and bool(settings.observium_csr_url),
            "last_sync_at": csr_source.last_sync_at if csr_source else None,
            "last_status": csr_source.last_status if csr_source else None,
        },
        "rancid": {
            "source": settings.rancid_path
            or (f"sftp://{settings.rancid_ssh_host}{settings.rancid_remote_path}" if settings.rancid_ssh_host else None),
            "configured": bool(settings.rancid_path or (settings.rancid_ssh_host and settings.rancid_ssh_user)),
        },
        "oxidized": {"url": settings.oxidized_url, "configured": bool(settings.oxidized_url)},
        "gitweb": {
            "url": settings.gitweb_url,
            "configured": bool(settings.gitweb_url),
            "files": db.scalar(select(func.count()).select_from(m.GitwebFile)) or 0,
            "devices_from_gitweb": db.scalar(select(func.count(func.distinct(m.GitwebFile.device_id)))
                                             .where(m.GitwebFile.created_device.is_(True))) or 0,
            "last_synced_at": db.scalar(select(func.max(m.GitwebFile.synced_at))),
            "schedule": {
                "enabled": settings.scheduler_enabled and bool(settings.gitweb_daily_at),
                "daily_at": settings.gitweb_daily_at,
                "timezone": settings.schedule_timezone,
            },
        },
        "rrd": {
            "mode": settings.observium_rrd_source,
            "path": settings.observium_rrd_path,
            "configured": {
                "api": bool(settings.observium_token or (settings.observium_user and settings.observium_password)),
                "ssh": bool(settings.observium_ssh_user),
                "local": True,
            }[settings.observium_rrd_source],
            "mapped_ports": db.scalar(select(func.count()).select_from(m.ObserviumPort)) or 0,
        },
        "parts": sorted(ALL_PARTS),
    }


class SettingsIn(BaseModel):
    values: dict[str, Any] = Field(default_factory=dict, description="Key -> new value; '' on a secret keeps it")
    clear: list[str] = Field(default_factory=list, description="Keys to remove (back to backend/.env)")


@router.get("/settings")
def get_settings(db: Session = Depends(get_db)):
    """Connection settings with their source (ui / env / default). Secrets are never returned."""
    return collector_settings.describe(db)


@router.put("/settings")
def put_settings(payload: SettingsIn, db: Session = Depends(get_db)):
    try:
        collector_settings.update(db, payload.values, payload.clear)
    except collector_settings.SettingsError as exc:
        code = 403 if "dimatikan" in str(exc) else 422
        raise HTTPException(code, str(exc)) from exc
    return collector_settings.describe(db)


@router.post("/settings/test")
def test_connection(target: str = Query(..., pattern="^(observium|observium_csr|gitweb)$")):
    """Try the saved settings: one Observium API call / the gitweb repository list. Read-only."""
    try:
        if target in ("observium", "observium_csr"):
            url = settings.observium_csr_url if target == "observium_csr" else settings.observium_url
            with ObserviumClient(base_url=url) as client:
                data = client._get("/devices/", {"pagesize": 1, "pageno": 1})
            return {"ok": True, "detail": f"Terhubung ke {url}: {data.get('count', '?')} perangkat"}
        client = GitwebClient()
        try:
            repos = client.repos()
        finally:
            client.close()
        return {"ok": True, "detail": f"Terhubung ke {settings.gitweb_url}: {len(repos)} repo RANCID"}
    except (ObserviumError, GitwebError) as exc:
        return {"ok": False, "detail": str(exc)}


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


@router.post("/observium-csr/sync", status_code=202)
def observium_csr_sync():
    """Start a background job on the CSR Observium: back up the DB, then its cell-site routers, ports and traffic."""
    try:
        client = ObserviumClient(base_url=settings.observium_csr_url)  # credentials checked now
    except ObserviumError as exc:
        raise HTTPException(502, str(exc)) from exc

    def job(db: Session, progress) -> dict:
        try:
            return jobs.run_observium_csr(db, client=client, progress=progress)
        finally:
            client.close()

    return _start("observium-csr", job)


@router.post("/observium-csr/rrd", status_code=202)
def observium_csr_rrd(days: int = Query(90, ge=7, le=365)):
    """Start a background job: daily traffic peaks of the links only the CSR Observium can see."""
    try:
        ObserviumClient(base_url=settings.observium_csr_url).close()  # credentials checked now
    except ObserviumError as exc:
        raise HTTPException(502, str(exc)) from exc

    def job(db: Session, progress) -> dict:
        return jobs.run_rrd_csr(db, days, progress=progress)

    return _start("observium-csr-rrd", job)


@router.post("/observium/rrd", status_code=202)
def observium_rrd(days: int = Query(90, ge=7, le=365)):
    """Start a background job: daily peak in/out for every link from Observium's RRD files over the
    last `days` days (replaces the link's samples in that window). Poll /collectors/jobs/current."""
    try:
        source = open_rrd_source()  # credentials / SSH access checked now, not inside the job
    except RrdError as exc:
        raise HTTPException(400 if "not configured" in str(exc) else 502, str(exc)) from exc

    def job(db: Session, progress) -> dict:
        try:
            return jobs.run_rrd(db, days, source=source, progress=progress)
        finally:
            source.close()

    return _start("observium-rrd", job)


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


@router.post("/configs/gitweb/sync", status_code=202)
def gitweb_sync(full: bool = Query(False, description="Re-read every file, not only those changed since the last run"),
                discover: bool = True):
    """Start a background job: configs of every RANCID repo in gitweb. Only files whose blob changed
    since the last run are downloaded; devices missing from the inventory (CSR) are created."""
    try:
        client = GitwebClient()
    except GitwebError as exc:
        raise HTTPException(400, str(exc)) from exc
    kind = "gitweb-full" if full else "gitweb"
    return _start(kind, lambda db, progress: jobs.run_gitweb(db, full, discover, client, progress))


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
