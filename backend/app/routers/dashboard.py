from collections import Counter

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import models as m
from ..database import get_db
from .capacity import all_link_capacity

router = APIRouter(prefix="/dashboard", tags=["Dashboard"])


def _count(db: Session, model) -> int:
    return db.scalar(select(func.count()).select_from(model)) or 0


@router.get("/summary")
def summary(db: Session = Depends(get_db)):
    links = all_link_capacity(db)

    used_ifaces = set()
    for link in db.scalars(select(m.Link)):
        used_ifaces.update(i for i in (link.a_interface_id, link.b_interface_id) if i)
    service_ports = {(sv.a_device_id, sv.a_interface) for sv in db.scalars(select(m.Service)) if sv.a_interface}
    available_ports = sum(
        1
        for i in db.scalars(select(m.Interface).where(m.Interface.if_type == "physical", m.Interface.parent_id.is_(None)))
        if i.id not in used_ifaces and (i.device_id, i.name) not in service_ports and i.admin_status == "up"
    )

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
        "available_ports": available_ports,
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
