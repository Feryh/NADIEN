from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models as m
from ..collectors.configs import parse_config
from ..collectors.configs.apply import apply_config
from ..collectors.configs.oxidized import OxidizedClient, os_hint, sync_oxidized
from ..collectors.observium.client import ObserviumClient, ObserviumError
from ..collectors.observium.sync import ALL_PARTS, SOURCE_NAME, sync_observium
from ..config import settings
from ..database import get_db
from ..services.topology import discover_links, norm_host

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
        "oxidized": {"url": settings.oxidized_url, "configured": bool(settings.oxidized_url)},
        "parts": sorted(ALL_PARTS),
    }


@router.post("/observium/sync")
def observium_sync(
    parts: list[str] = Query(default=sorted(ALL_PARTS)),
    discover: bool = True,
    db: Session = Depends(get_db),
):
    """Pull devices/ports/addresses (inventory), LLDP (neighbours) and port rates (utilization) from Observium."""
    unknown = set(parts) - ALL_PARTS
    if unknown:
        raise HTTPException(422, f"Unknown parts: {', '.join(sorted(unknown))}")
    try:
        with ObserviumClient() as client:
            return sync_observium(db, client, set(parts), discover).as_dict()
    except ObserviumError as exc:
        raise HTTPException(502, str(exc)) from exc


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


@router.post("/configs/oxidized/sync")
def oxidized_sync(discover: bool = True, db: Session = Depends(get_db)):
    """Fetch every device config from Oxidized and merge it into the inventory."""
    try:
        client = OxidizedClient()
    except RuntimeError as exc:
        raise HTTPException(400, str(exc)) from exc
    try:
        return sync_oxidized(db, client, discover)
    finally:
        client.close()


@router.post("/configs/{hostname}")
async def import_config(
    hostname: str,
    request: Request,
    vendor: str | None = None,
    discover: bool = Query(True, description="Re-run topology discovery so config-described links appear"),
    db: Session = Depends(get_db),
):
    """Upload one device's config (raw text body) and merge it into the inventory."""
    text = await _read_text(request)
    device = next((d for d in db.scalars(select(m.Device)) if norm_host(d.hostname) == norm_host(hostname)), None)
    if device is None:
        raise HTTPException(404, f"Device {hostname} not in inventory — run the Observium sync first")
    vendors = {v.id: v for v in db.scalars(select(m.Vendor))}
    try:
        cfg = parse_config(text, vendor or os_hint(device, vendors))
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    result = apply_config(db, device, cfg)
    if discover:
        result["discovery"] = discover_links(db).model_dump()
    return result
