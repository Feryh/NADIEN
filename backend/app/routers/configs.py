import re
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import PlainTextResponse
from sqlalchemy.orm import Session

from .. import models as m
from ..database import get_db
from ..services.configgen import AddressError, Addressing, as_text, generate, parse_cidr, parse_vlans

router = APIRouter(prefix="/configs", tags=["Config Generator"])


def addressing_params(
    a_ip: str | None = Query(None, max_length=18, description="Alamat PE site A, CIDR (10.10.10.1/30)"),
    b_ip: str | None = Query(None, max_length=18, description="Alamat PE site B, CIDR"),
    vc_id: int | None = Query(None, ge=1, le=4294967295, description="Pseudowire/VC id; default = VLAN"),
    bgp_asn: int | None = Query(None, ge=1, le=4294967295, description="ASN BGP perangkat (L3VPN); default dari RD"),
    policer: bool = Query(False, description="Tambahkan policer sesuai bandwidth layanan"),
    vlans: str | None = Query(None, max_length=256, description="VPLS beberapa VLAN, mis. 1480-1485,1489"),
) -> Addressing:
    """Shared by the config generator and the MoP routes; bad addresses are a 422 before anything is built."""
    try:
        parse_cidr(a_ip)
        parse_cidr(b_ip)
        vlan_list = parse_vlans(vlans)
    except AddressError as exc:
        raise HTTPException(422, str(exc)) from exc
    return Addressing(a_ip or None, b_ip or None, vc_id, bgp_asn, policer, vlan_list)


def _service(db: Session, service_id: int) -> m.Service:
    service = db.get(m.Service, service_id)
    if service is None:
        raise HTTPException(404, "Service not found")
    return service


@router.get("/services/{service_id}")
def service_config(service_id: int, addressing: Addressing = Depends(addressing_params), db: Session = Depends(get_db)):
    """Configuration and rollback per endpoint in each device's own dialect."""
    return generate(db, _service(db, service_id), addressing)


@router.get("/services/{service_id}/text", response_class=PlainTextResponse)
def service_config_text(service_id: int, side: Literal["A", "B"] = Query(...),
                        part: Literal["config", "rollback"] = Query("config"),
                        addressing: Addressing = Depends(addressing_params), db: Session = Depends(get_db)):
    """One site's config or rollback as a plain file to paste: commands and commit only, no comments."""
    service = _service(db, service_id)
    result = generate(db, service, addressing)
    device = result["endpoints"][side]["device"] or f"site{side}"
    filename = re.sub(r"[^A-Za-z0-9._-]+", "_", f"{part}-{service.circuit_id}-{device}") + ".txt"
    return PlainTextResponse(as_text(result, side, part),
                             headers={"Content-Disposition": f'attachment; filename="{filename}"'})
