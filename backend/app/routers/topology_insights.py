"""Live topology (signature + change feed) and the naming standard report, next to the drill-down views."""

import csv
import io
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from .. import models as m
from .. import schemas as s
from ..database import get_db
from ..services import cache
from ..services.auth import CurrentUser, require
from ..services.naming_report import Inventory, device_rows, device_script, port_rows, summary
from .analysis import _context

router = APIRouter(prefix="/topology", tags=["Topology"])

EventKind = Literal["device_added", "link_added", "link_down", "link_up", "discovery"]


# --------------------------------------------------------------------------- live updates


@router.get("/signature")
def topology_signature(db: Session = Depends(get_db)):
    """Cheap fingerprint of what the map shows: devices, links, their status and traffic. The topology page polls
    it and reloads only when it changes, so new devices and links appear without a manual refresh."""
    devices = db.execute(select(func.count(m.Device.id), func.max(m.Device.id), func.max(m.Device.updated_at))).one()
    links = db.execute(select(func.count(m.Link.id), func.max(m.Link.id),
                              func.sum(case((m.Link.status == "down", 1), else_=0)),
                              func.sum(m.Link.current_gbps))).one()
    last_event = db.scalar(select(func.max(m.TopologyEvent.id))) or 0
    parts = [devices[0], devices[1] or 0, str(devices[2] or ""), links[0], links[1] or 0, links[2] or 0,
             round(links[3] or 0), last_event]
    return {"signature": "|".join(str(p) for p in parts), "devices": devices[0], "links": links[0],
            "links_down": links[2] or 0, "last_event_id": last_event}


@router.get("/events")
def topology_events(since: datetime | None = None, kind: EventKind | None = None,
                    limit: int = Query(200, ge=1, le=1000), db: Session = Depends(get_db)):
    """Newest first: links added / down / up (from discovery) and devices added (from the inventory).
    Default window: the last 7 days."""
    since = since or datetime.now(timezone.utc) - timedelta(days=7)
    if since.tzinfo is None:
        since = since.replace(tzinfo=timezone.utc)
    rows: list[dict[str, Any]] = []
    if kind in (None, "device_added"):
        pops = dict(db.execute(select(m.Pop.id, m.Pop.code)).tuples().all())
        for d in db.execute(select(m.Device.id, m.Device.hostname, m.Device.role, m.Device.pop_id, m.Device.created_at)
                            .where(m.Device.created_at >= since).order_by(m.Device.created_at.desc()).limit(limit)):
            rows.append({"id": f"device:{d.id}", "at": d.created_at, "kind": "device_added", "title": d.hostname,
                         "detail": f"{(d.role or '-').upper()} · POP {pops.get(d.pop_id, '-')}",
                         "device_id": d.id, "link_id": None})
    if kind != "device_added":
        stmt = select(m.TopologyEvent).where(m.TopologyEvent.at >= since)
        if kind:
            stmt = stmt.where(m.TopologyEvent.kind == kind)
        for e in db.scalars(stmt.order_by(m.TopologyEvent.id.desc()).limit(limit)):
            rows.append({"id": f"event:{e.id}", "at": e.at, "kind": e.kind, "title": e.title, "detail": e.detail,
                         "device_id": e.device_id, "link_id": e.link_id})
    rows.sort(key=lambda r: _aware(r["at"]), reverse=True)
    return rows[:limit]


def _aware(value: datetime | None) -> datetime:
    if value is None:
        return datetime.min.replace(tzinfo=timezone.utc)
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


# --------------------------------------------------------------------------- naming standard


def _inventory(db: Session) -> Inventory:
    ctx = _context(db)
    return Inventory(
        devices=ctx.devices,
        pop_code={d: ctx.pop_code(d) for d in ctx.devices},
        area={d: ctx.area_of_device(d) for d in ctx.devices},
        vendors={v.id: v for v in db.execute(select(m.Vendor.id, m.Vendor.name, m.Vendor.os_family))},
    )


def _devices(db: Session) -> list[dict[str, Any]]:
    return cache.cached(("naming", "devices"), lambda: device_rows(_inventory(db)))


def _ports(db: Session) -> list[dict[str, Any]]:
    return cache.cached(("naming", "ports"), lambda: port_rows(db, _inventory(db)))


def _match(row: dict[str, Any], q: str | None, fields: tuple[str, ...]) -> bool:
    if not q:
        return True
    needle = q.strip().lower()
    return any(needle in str(row.get(f) or "").lower() for f in fields)


def _filter_devices(db, status, area, q):
    return [r for r in _devices(db) if (not status or r["status"] == status) and (not area or r["area"] == area)
            and _match(r, q, ("hostname", "pop", "suggestion", "vendor"))]


def _filter_ports(db, status, segment, area, q):
    return [r for r in _ports(db) if (not status or r["status"] == status) and (not segment or r["segment"] == segment)
            and (not area or r["area"] == area) and _match(r, q, ("hostname", "port", "peer", "description"))]


@router.get("/naming")
def naming_summary(db: Session = Depends(get_db)):
    return summary(_devices(db), _ports(db))


@router.get("/naming/devices")
def naming_devices(status: Literal["standard", "needs_fix", "legacy", "non_standard"] | None = None,
                   area: str | None = Query(None, max_length=64), q: str | None = Query(None, max_length=100),
                   limit: int = Query(100, ge=1, le=100000), offset: int = Query(0, ge=0),
                   db: Session = Depends(get_db)):
    rows = _filter_devices(db, status, area, q)
    return {"total": len(rows), "rows": rows[offset:offset + limit]}


@router.get("/naming/ports")
def naming_ports(status: Literal["standard", "names_peer", "no_peer", "empty"] | None = None,
                 segment: Literal["backbone", "distribution", "last_mile"] | None = None,
                 area: str | None = Query(None, max_length=64), q: str | None = Query(None, max_length=100),
                 limit: int = Query(100, ge=1, le=100000), offset: int = Query(0, ge=0),
                 db: Session = Depends(get_db)):
    rows = _filter_ports(db, status, segment, area, q)
    return {"total": len(rows), "rows": rows[offset:offset + limit]}


def _csv(name: str, header: list[str], rows: list[list[Any]]) -> StreamingResponse:
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(header)
    writer.writerows(rows)
    return StreamingResponse(iter([buffer.getvalue()]), media_type="text/csv",
                             headers={"Content-Disposition": f'attachment; filename="{name}"'})


@router.get("/naming/devices/export")
def export_naming_devices(status: str | None = Query(None, max_length=16), area: str | None = Query(None, max_length=64),
                          q: str | None = Query(None, max_length=100), db: Session = Depends(get_db)):
    rows = _filter_devices(db, status, area, q)
    return _csv("nadien-penamaan-perangkat.csv",
                ["hostname", "area", "pop", "role", "vendor", "status", "masalah", "usulan"],
                [[r["hostname"], r["area"], r["pop"], r["role"], r["vendor"], r["status_label"],
                  "; ".join(r["issues"]), r["suggestion"] or ""] for r in rows])


@router.get("/naming/ports/export")
def export_naming_ports(status: str | None = Query(None, max_length=16), segment: str | None = Query(None, max_length=16),
                        area: str | None = Query(None, max_length=64), q: str | None = Query(None, max_length=100),
                        db: Session = Depends(get_db)):
    rows = _filter_ports(db, status, segment, area, q)
    return _csv("nadien-deskripsi-port.csv",
                ["hostname", "port", "segmen", "lawan", "port lawan", "deskripsi sekarang", "status", "rekomendasi"],
                [[r["hostname"], r["port"], r["segment_label"], r["peer"], r["peer_port"] or "", r["description"] or "",
                  r["status_label"], r["recommended"]] for r in rows])


@router.get("/naming/devices/{device_id}/script")
def naming_device_script(device_id: int, db: Session = Depends(get_db)):
    inv = _inventory(db)
    if device_id not in inv.devices:
        raise HTTPException(404, "Device not found")
    return device_script(_ports(db), device_id, inv)


@router.get("/lldp-unknown-peers")
def lldp_unknown_peers(db: Session = Depends(get_db)):
    """Ports whose LLDP neighbour is outside the inventory while their description names an inventory device."""
    from ..services.topology import unknown_lldp_peers  # late: discovery helpers load every port

    return cache.cached(("topology", "lldp-unknown-peers"), lambda: unknown_lldp_peers(db))


@router.post("/link-approvals", status_code=201)
def approve_links(payload: s.LinkApprovalsIn, user: CurrentUser = Depends(require("approver")),
                  db: Session = Depends(get_db)):
    """Confirm description pairs LLDP cannot see (a carrier/transport box in between). Each pair is drawn at once as
    an "approved" link and kept by discovery until revoked or until LLDP sees the two ports directly."""
    from ..services.topology import ApprovalError, approve_link  # late: discovery helpers load every port

    try:
        links = [approve_link(db, p.interface_id, p.peer_interface_id, user.username) for p in payload.pairs]
    except ApprovalError as error:
        db.rollback()  # all or nothing: one bad pair approves none of them
        raise HTTPException(error.status, str(error)) from error
    db.commit()
    return {"approved": len(links), "link_ids": links}


@router.delete("/link-approvals/{approval_id}", status_code=204)
def revoke_link_approval(approval_id: int, _: CurrentUser = Depends(require("approver")),
                         db: Session = Depends(get_db)):
    from ..services.topology import revoke_link

    if not revoke_link(db, approval_id):
        raise HTTPException(404, "Persetujuan tidak ditemukan")
    db.commit()
