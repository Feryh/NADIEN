from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models as m
from ..database import get_db
from ..services import cache
from ..services.validation import ServiceDesign, design_for_service, validate

router = APIRouter(prefix="/validation", tags=["Service Validation"])


class DesignIn(BaseModel):
    """A service design, stored or not: what the Customer Path page has before reserving."""

    service_type: Literal["internet", "iplc", "l2vpn", "l3vpn"]
    bandwidth_mbps: int = Field(gt=0)
    protection: Literal["none", "1+1", "1:1"] = "none"
    a_device_id: int | None = None
    a_interface: str | None = Field(default=None, max_length=64)
    b_device_id: int | None = None
    b_interface: str | None = Field(default=None, max_length=64)
    vlan_id: int | None = Field(default=None, ge=1, le=4094)
    vrf_name: str | None = Field(default=None, max_length=64)
    route_distinguisher: str | None = Field(default=None, max_length=64)
    route_target: str | None = Field(default=None, max_length=255)
    primary_link_ids: list[int] = Field(default_factory=list, max_length=200)
    backup_link_ids: list[int] = Field(default_factory=list, max_length=200)


@router.post("/check")
def check_design(payload: DesignIn, db: Session = Depends(get_db)):
    """Validate a design before it is reserved (nothing is written)."""
    return validate(db, ServiceDesign(**payload.model_dump()))


@router.get("/services/{service_id}")
def check_service(service_id: int, db: Session = Depends(get_db)):
    """Validate a stored service with its engineered paths."""
    service = db.get(m.Service, service_id)
    if service is None:
        raise HTTPException(404, "Service not found")
    report = validate(db, design_for_service(db, service))
    return {**_summary(service), **report}


@router.get("/services")
def check_all(status: str | None = Query(None, pattern="^(fail|warn|pass)$"), db: Session = Depends(get_db)):
    """Every service that is not terminated, with its overall result (worst first)."""
    rows = cache.cached(("validation", "all"), lambda: _all(db))
    if status:
        rows = [r for r in rows if r["status"] == status]
    return {"counts": {s: sum(1 for r in rows if r["status"] == s) for s in ("fail", "warn", "pass")}, "services": rows}


def _all(db: Session) -> list[dict]:
    rows = []
    for service in db.scalars(select(m.Service).where(m.Service.status != "terminated").order_by(m.Service.id.desc())):
        report = validate(db, design_for_service(db, service))
        rows.append({**_summary(service), "status": report["status"], "counts": report["counts"],
                     "failed": [c["title"] for g in report["groups"] for c in g["checks"] if c["status"] == "fail"][:5]})
    order = {"fail": 0, "warn": 1, "pass": 2}
    return sorted(rows, key=lambda r: order[r["status"]])


def _summary(service: m.Service) -> dict:
    return {"service_id": service.id, "circuit_id": service.circuit_id, "service_type": service.service_type,
            "bandwidth_mbps": service.bandwidth_mbps, "protection": service.protection, "service_status": service.status,
            "vlan_id": service.vlan_id, "vrf_name": service.vrf_name}
