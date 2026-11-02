from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from .. import models as m
from .. import schemas as s
from ..database import get_db
from ..services.capacity import calculate
from ..services.topology import discover_links, norm_host

router = APIRouter(prefix="/topology", tags=["Topology"])


@router.get("")
def get_topology(db: Session = Depends(get_db)):
    """Graph for the Metro map: POPs (groups), devices (nodes) and links (edges with capacity color)."""
    pops = db.scalars(select(m.Pop)).all()
    devices = db.scalars(select(m.Device)).all()
    vendors = {v.id: v.name for v in db.scalars(select(m.Vendor))}
    ifaces = {i.id: i.name for i in db.scalars(select(m.Interface).where(m.Interface.if_type != "subinterface"))}
    links = db.scalars(select(m.Link)).all()

    edges = []
    for link in links:
        cap = calculate(link.capacity_gbps, link.current_gbps, link.reserved_gbps)
        edges.append({
            "id": link.id,
            "source": link.a_device_id,
            "target": link.b_device_id,
            "name": link.name,
            "a_interface": ifaces.get(link.a_interface_id or -1),
            "b_interface": ifaces.get(link.b_interface_id or -1),
            "link_type": link.link_type,
            "status": link.status,
            "discovery_source": link.discovery_source,
            "capacity_gbps": cap.capacity_gbps,
            "current_gbps": cap.current_gbps,
            "reserved_gbps": cap.reserved_gbps,
            "utilization_pct": cap.utilization_pct,
            "committed_pct": cap.committed_pct,
            "color": cap.color,
            "latency_ms": link.latency_ms,
        })

    return {
        "pops": [
            {"id": p.id, "code": p.code, "name": p.name, "tier": p.tier, "latitude": p.latitude, "longitude": p.longitude}
            for p in pops
        ],
        "devices": [
            {
                "id": d.id,
                "hostname": d.hostname,
                "pop_id": d.pop_id,
                "role": d.role,
                "vendor": vendors.get(d.vendor_id or -1),
                "status": d.status,
                "loopback_ipv4": d.loopback_ipv4,
                "x": d.map_x,
                "y": d.map_y,
            }
            for d in devices
        ],
        "links": edges,
    }


@router.put("/positions", status_code=204)
def save_positions(positions: list[s.NodePosition], db: Session = Depends(get_db)):
    for pos in positions:
        device = db.get(m.Device, pos.device_id)
        if device is not None:
            device.map_x, device.map_y = pos.x, pos.y
    db.commit()


@router.delete("/positions", status_code=204)
def reset_positions(db: Session = Depends(get_db)):
    for device in db.scalars(select(m.Device)):
        device.map_x = device.map_y = None
    db.commit()


@router.get("/lldp", response_model=list[s.LldpNeighborRead])
def list_lldp(device_id: int | None = None, db: Session = Depends(get_db)):
    stmt = select(m.LldpNeighbor).order_by(m.LldpNeighbor.device_id, m.LldpNeighbor.local_interface)
    if device_id is not None:
        stmt = stmt.where(m.LldpNeighbor.device_id == device_id)
    return db.scalars(stmt).all()


@router.post("/lldp")
def ingest_lldp(tables: list[s.LldpTableIn], db: Session = Depends(get_db)):
    """Replace the LLDP neighbor table of each given device (output of a collector run)."""
    by_host = {norm_host(d.hostname): d for d in db.scalars(select(m.Device))}
    unknown, stored = [], 0
    for table in tables:
        device = by_host.get(norm_host(table.hostname))
        if device is None:
            unknown.append(table.hostname)
            continue
        db.execute(delete(m.LldpNeighbor).where(m.LldpNeighbor.device_id == device.id))
        for n in table.neighbors:
            db.add(m.LldpNeighbor(device_id=device.id, **n.model_dump()))
            stored += 1
    db.commit()
    if unknown and not stored:
        raise HTTPException(404, f"Unknown devices: {', '.join(unknown)}")
    return {"stored": stored, "unknown_devices": unknown}


@router.post("/discover", response_model=s.DiscoveryResult)
def run_discovery(db: Session = Depends(get_db)):
    """Rebuild the link inventory from LLDP (mandatory) with config descriptions as backup."""
    return discover_links(db)
