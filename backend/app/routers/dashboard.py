from collections import Counter

from fastapi import APIRouter, Depends
from sqlalchemy import func, select, union
from sqlalchemy.orm import Session

from .. import models as m
from ..database import get_db
from ..services import cache
from .capacity import all_link_capacity

router = APIRouter(prefix="/dashboard", tags=["Dashboard"])


def _count(db: Session, model) -> int:
    return db.scalar(select(func.count()).select_from(model)) or 0


def _available_ports(db: Session) -> int:
    """Enabled physical ports (not bundle members) carrying neither a link nor a service — counted in SQL;
    there are 300k+ interfaces, far too many to load as objects per dashboard view."""
    linked = union(
        select(m.Link.a_interface_id.label("iface_id")).where(m.Link.a_interface_id.is_not(None)),
        select(m.Link.b_interface_id.label("iface_id")).where(m.Link.b_interface_id.is_not(None)),
    ).subquery()
    free = (m.Interface.if_type == "physical", m.Interface.parent_id.is_(None), m.Interface.admin_status == "up",
            m.Interface.id.not_in(select(linked.c.iface_id)))
    total = db.scalar(select(func.count()).select_from(m.Interface).where(*free)) or 0
    service_ports = {(sv.a_device_id, sv.a_interface) for sv in db.execute(
        select(m.Service.a_device_id, m.Service.a_interface).where(m.Service.a_interface.is_not(None)))}
    if not service_ports:
        return total
    taken = db.execute(select(m.Interface.device_id, m.Interface.name).where(
        *free, m.Interface.device_id.in_({d for d, _ in service_ports}))).all()
    return total - sum(1 for row in taken if (row.device_id, row.name) in service_ports)


@router.get("/summary")
def summary(db: Session = Depends(get_db)):
    return cache.cached(("dashboard", "summary"), lambda: _summary(db))


def _summary(db: Session) -> dict:
    links = all_link_capacity(db)
    services = db.scalars(select(m.Service).where(m.Service.status != "terminated")).all()
    total_cap = sum(l.capacity_gbps for l in links)
    total_cur = sum(l.current_gbps for l in links)
    total_res = sum(l.reserved_gbps for l in links)

    return {
        "counts": {
            "pops": _count(db, m.Pop),
            "devices": _count(db, m.Device),
            "interfaces": _count(db, m.Interface),
            "links": len(links),
            "customers": _count(db, m.Customer),
            "services": len(services),
            "fibers": _count(db, m.Fiber),
            "dwdm_nodes": _count(db, m.DwdmNode),
        },
        "available_ports": _available_ports(db),
        "capacity": {
            "total_gbps": round(total_cap, 1),
            "current_gbps": round(total_cur, 1),
            "reserved_gbps": round(total_res, 1),
            "available_gbps": round(max(total_cap - total_cur - total_res, 0), 1),
            "utilization_pct": round(total_cur / total_cap * 100, 1) if total_cap else 0,
        },
        "link_colors": dict(Counter(l.color for l in links)),
        "links_down": sum(1 for l in links if l.status == "down"),
        "services_by_type": dict(Counter(sv.service_type for sv in services)),
        "bandwidth_by_type_mbps": {
            t: sum(sv.bandwidth_mbps for sv in services if sv.service_type == t)
            for t in {sv.service_type for sv in services}
        },
        "exhausting_soon": [
            l.model_dump()
            for l in sorted(
                (l for l in links if l.months_to_threshold is not None),
                key=lambda l: l.months_to_threshold,
            )[:5]
        ],
    }
