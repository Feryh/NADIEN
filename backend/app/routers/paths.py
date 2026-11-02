from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .. import models as m
from ..collectors import runner as jobs_runner
from ..database import get_db
from ..services.paths import build_graph, compute

router = APIRouter(prefix="/paths", tags=["Customer Path"])


class Endpoint(BaseModel):
    """A site: one device, or a POP (any of its devices may terminate the service)."""

    device_id: int | None = None
    pop: str | None = None

    @model_validator(mode="after")
    def _one(self) -> "Endpoint":
        if (self.device_id is None) == (not self.pop):
            raise ValueError("give either device_id or pop")
        return self


class PathRequest(BaseModel):
    a: Endpoint
    b: Endpoint
    bandwidth_mbps: int = Field(gt=0)
    protection: Literal["none", "1+1", "1:1"] = "none"
    max_util_pct: float | None = Field(default=None, gt=0, le=100)
    metric: Literal["latency", "hops"] = "latency"


class ReserveRequest(PathRequest):
    customer_id: int
    circuit_id: str = Field(min_length=1, max_length=64)
    service_type: Literal["internet", "iplc", "l2vpn", "l3vpn"]
    vlan_id: int | None = Field(default=None, ge=1, le=4094)
    vrf_name: str | None = None
    notes: str | None = None
    allow_unprotected: bool = False


def _resolve(db: Session, ep: Endpoint) -> tuple[set[int], str]:
    if ep.device_id is not None:
        device = db.get(m.Device, ep.device_id)
        if device is None:
            raise HTTPException(404, f"Device {ep.device_id} not found")
        return {device.id}, device.hostname
    pop = db.scalar(select(m.Pop).where(m.Pop.code == ep.pop.upper()))
    if pop is None:
        raise HTTPException(404, f"POP {ep.pop} not found")
    ids = set(db.scalars(select(m.Device.id).where(m.Device.pop_id == pop.id, m.Device.status != "decommissioned")))
    if not ids:
        raise HTTPException(422, f"POP {pop.code} has no devices")
    return ids, f"POP {pop.code}"


def _compute(db: Session, req: PathRequest) -> dict:
    a, a_label = _resolve(db, req.a)
    b, b_label = _resolve(db, req.b)
    result = compute(build_graph(db), a, b, req.bandwidth_mbps / 1000, req.protection, req.max_util_pct, req.metric)
    return {"a": a_label, "b": b_label, **result}


@router.post("/compute")
def compute_path(req: PathRequest, db: Session = Depends(get_db)):
    """Recommended primary path, a diverse backup when protected, latency, hops and per-link capacity check."""
    return _compute(db, req)


def _path_row(service_id: int, role: str, path: dict, reserved: float) -> m.ServicePath:
    return m.ServicePath(service_id=service_id, role=role, device_ids=path["device_ids"], link_ids=path["link_ids"],
                         hops=path["hops"], latency_ms=path["latency_ms"],
                         latency_estimated=path["latency_estimated"], reserved_gbps=reserved)


@router.post("/reserve", status_code=201)
def reserve_path(req: ReserveRequest, db: Session = Depends(get_db)):
    """Recompute (never trust a stale result), then create the service as `reserved` and add its
    bandwidth to `reserved_gbps` of every primary link — and backup links for 1+1."""
    jobs_runner.ensure_idle(jobs_runner.runner)
    if db.get(m.Customer, req.customer_id) is None:
        raise HTTPException(404, f"Customer {req.customer_id} not found")
    result = _compute(db, req)
    if result["primary"] is None:
        raise HTTPException(409, {"message": "Tidak ada jalur yang memenuhi kapasitas", "result": result})
    if req.protection != "none" and result["backup"] is None and not req.allow_unprotected:
        raise HTTPException(409, {"message": "Proteksi diminta tetapi tidak ada jalur backup yang disjoint", "result": result})

    bw = req.bandwidth_mbps / 1000
    primary, backup = result["primary"], result["backup"]
    devices = {d.id: d for d in db.scalars(select(m.Device).where(
        m.Device.id.in_([primary["device_ids"][0], primary["device_ids"][-1]])))}
    a_dev, b_dev = devices[primary["device_ids"][0]], devices[primary["device_ids"][-1]]
    service = m.Service(
        customer_id=req.customer_id, circuit_id=req.circuit_id, service_type=req.service_type,
        bandwidth_mbps=req.bandwidth_mbps, a_pop_id=a_dev.pop_id, a_device_id=a_dev.id,
        b_pop_id=b_dev.pop_id, b_device_id=b_dev.id, vlan_id=req.vlan_id, vrf_name=req.vrf_name,
        protection=req.protection, status="reserved", notes=req.notes,
    )
    db.add(service)
    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(409, f"Circuit ID {req.circuit_id} sudah dipakai") from exc

    reservations = [("primary", primary, bw)]
    if backup is not None:
        reservations.append(("backup", backup, bw if req.protection == "1+1" else 0.0))
    for role, path, amount in reservations:
        db.add(_path_row(service.id, role, path, amount))
        if amount:
            for link in db.scalars(select(m.Link).where(m.Link.id.in_(path["link_ids"]))):
                link.reserved_gbps = round((link.reserved_gbps or 0) + amount, 6)
    db.commit()
    return {"service_id": service.id, "circuit_id": service.circuit_id, "status": service.status, **result}


def _service_paths(db: Session, service: m.Service) -> dict:
    paths = db.scalars(select(m.ServicePath).where(m.ServicePath.service_id == service.id)).all()
    hostnames = {d.id: d.hostname for d in db.scalars(select(m.Device).where(
        m.Device.id.in_({i for p in paths for i in p.device_ids})))}
    return {
        "service_id": service.id, "circuit_id": service.circuit_id, "status": service.status,
        "service_type": service.service_type, "bandwidth_mbps": service.bandwidth_mbps,
        "protection": service.protection, "customer_id": service.customer_id,
        "paths": [{
            "role": p.role, "hops": p.hops, "latency_ms": p.latency_ms, "latency_estimated": p.latency_estimated,
            "reserved_gbps": p.reserved_gbps, "released": p.released, "link_ids": p.link_ids,
            "devices": [hostnames.get(i, f"#{i}") for i in p.device_ids],
        } for p in paths],
    }


@router.get("/services")
def list_engineered_services(db: Session = Depends(get_db)):
    """Services that have an engineered path, newest first."""
    ids = set(db.scalars(select(m.ServicePath.service_id)))
    services = db.scalars(select(m.Service).where(m.Service.id.in_(ids)).order_by(m.Service.id.desc())).all()
    return [_service_paths(db, s) for s in services]


@router.get("/services/{service_id}")
def get_service_paths(service_id: int, db: Session = Depends(get_db)):
    service = db.get(m.Service, service_id)
    if service is None:
        raise HTTPException(404, "Service not found")
    return _service_paths(db, service)


@router.post("/services/{service_id}/release")
def release_service(service_id: int, db: Session = Depends(get_db)):
    """Give the reserved bandwidth back to every link and mark the service terminated."""
    jobs_runner.ensure_idle(jobs_runner.runner)
    service = db.get(m.Service, service_id)
    if service is None:
        raise HTTPException(404, "Service not found")
    released = 0.0
    for path in db.scalars(select(m.ServicePath).where(m.ServicePath.service_id == service_id,
                                                        m.ServicePath.released.is_(False))):
        if path.reserved_gbps:
            for link in db.scalars(select(m.Link).where(m.Link.id.in_(path.link_ids))):
                link.reserved_gbps = max(round((link.reserved_gbps or 0) - path.reserved_gbps, 6), 0.0)
            released += path.reserved_gbps * len(path.link_ids)
        path.released = True
    service.status = "terminated"
    db.commit()
    return {"service_id": service_id, "status": service.status, "released_gbps_link_total": round(released, 3)}
