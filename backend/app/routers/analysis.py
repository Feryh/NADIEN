"""Read models for a large network: areas, a drill-down topology, and port capacity usage.

Everything is aggregated per request from devices/links/interfaces (thousands of devices,
hundreds of thousands of ports); interfaces are grouped in SQL first.
"""

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import models as m
from ..database import get_db
from ..services.areas import (
    AREA_ORDER,
    CATEGORY_LABELS,
    ROLE_TIER,
    UNMAPPED_AREA,
    area_for_pop,
    area_position,
    bucket_sort_key,
    category_for,
    site_for,
    speed_bucket,
)
from ..services.capacity import capacity_color

router = APIRouter(tags=["Analysis"])


# --------------------------------------------------------------------------- shared context


@dataclass
class _Ctx:
    pops: dict[int, m.Pop]
    area_of_pop: dict[int, str]
    devices: dict[int, Any]  # rows: id, hostname, pop_id, role, vendor_id, status
    vendors: dict[int, str]

    def area_of_device(self, device_id: int) -> str:
        return self.area_of_pop.get(self.devices[device_id].pop_id, UNMAPPED_AREA)

    def pop_code(self, device_id: int) -> str:
        pop = self.pops.get(self.devices[device_id].pop_id)
        return pop.code if pop else "?"


def _context(db: Session) -> _Ctx:
    pops = {p.id: p for p in db.scalars(select(m.Pop))}
    devices = {
        r.id: r
        for r in db.execute(select(m.Device.id, m.Device.hostname, m.Device.pop_id, m.Device.role,
                                   m.Device.vendor_id, m.Device.status, m.Device.map_x, m.Device.map_y))
    }
    return _Ctx(
        pops=pops,
        area_of_pop={pid: area_for_pop(p.code, p.region, p.name) for pid, p in pops.items()},
        devices=devices,
        vendors={v.id: v.name for v in db.scalars(select(m.Vendor))},
    )


@dataclass
class _Agg:
    links: int = 0
    down: int = 0
    capacity: float = 0.0
    current: float = 0.0
    reserved: float = 0.0

    def add(self, link: Any) -> None:
        self.links += 1
        self.down += link.status == "down"
        self.capacity += link.capacity_gbps or 0
        self.current += link.current_gbps or 0
        self.reserved += link.reserved_gbps or 0

    def as_dict(self) -> dict[str, Any]:
        committed = (self.current + self.reserved) / self.capacity * 100 if self.capacity else 0.0
        return {
            "links": self.links,
            "down": self.down,
            "capacity_gbps": round(self.capacity, 1),
            "current_gbps": round(self.current, 1),
            "utilization_pct": round(self.current / self.capacity * 100, 1) if self.capacity else 0.0,
            "committed_pct": round(committed, 1),
            "color": capacity_color(committed),
        }


def _links(db: Session):
    return db.execute(select(m.Link.id, m.Link.a_device_id, m.Link.b_device_id, m.Link.capacity_gbps,
                             m.Link.current_gbps, m.Link.reserved_gbps, m.Link.status)).all()


def _edges(aggs: dict[tuple[str, str], _Agg]) -> list[dict[str, Any]]:
    return [{"id": f"{a}|{b}", "source": a, "target": b, **agg.as_dict()} for (a, b), agg in aggs.items()]


def _pair(a: str, b: str) -> tuple[str, str]:
    return (a, b) if a <= b else (b, a)


# --------------------------------------------------------------------------- areas


@router.get("/areas")
def list_areas(db: Session = Depends(get_db)):
    ctx = _context(db)
    pops_per_area: Counter = Counter(ctx.area_of_pop.values())
    devices_per_area: Counter = Counter(ctx.area_of_device(d) for d in ctx.devices)
    unmapped = sorted(p.code for pid, p in ctx.pops.items() if ctx.area_of_pop[pid] == UNMAPPED_AREA)
    areas = [a for a in AREA_ORDER if pops_per_area[a]] + sorted(set(pops_per_area) - set(AREA_ORDER))
    return {
        "areas": [{"area": a, "pops": pops_per_area[a], "devices": devices_per_area[a]} for a in areas],
        "unmapped_pops": unmapped,
    }


@router.post("/areas/auto-assign")
def auto_assign_areas(db: Session = Depends(get_db)):
    """Fill `region` on POPs that have none, from the city-code map or city/province words in the
    POP name. Never overwrites a region that is already set."""
    assigned: Counter = Counter()
    for pop in db.scalars(select(m.Pop)):
        if (pop.region or "").strip():
            continue
        area = area_for_pop(pop.code, None, pop.name)
        area = None if area == UNMAPPED_AREA else area
        if area:
            pop.region = area
            assigned[area] += 1
    db.commit()
    return {"assigned": sum(assigned.values()), "by_area": dict(assigned)}


# --------------------------------------------------------------------------- drill-down topology


@router.get("/topology/view")
def topology_view(
    level: str = Query("area", pattern="^(area|pop|device)$"),
    area: str | None = None,
    pop: str | None = None,
    db: Session = Depends(get_db),
):
    """area: areas + inter-area trunks · pop (needs `area`): POPs of one area + neighbouring areas ·
    device (needs `pop`): devices of one POP grouped by site + links out to other POPs."""
    ctx = _context(db)
    links = _links(db)
    if level == "area":
        return _area_level(ctx, links)
    if level == "pop":
        if not area:
            raise HTTPException(422, "area is required for level=pop")
        return _pop_level(ctx, links, area)
    if not pop:
        raise HTTPException(422, "pop is required for level=device")
    return _device_level(ctx, links, pop)


def _area_level(ctx: _Ctx, links) -> dict:
    inner: dict[str, _Agg] = defaultdict(_Agg)
    trunks: dict[tuple[str, str], _Agg] = defaultdict(_Agg)
    for link in links:
        if link.a_device_id not in ctx.devices or link.b_device_id not in ctx.devices:
            continue
        a, b = ctx.area_of_device(link.a_device_id), ctx.area_of_device(link.b_device_id)
        if a == b:
            inner[a].add(link)
        else:
            trunks[_pair(f"area:{a}", f"area:{b}")].add(link)
    pops_per_area: Counter = Counter(ctx.area_of_pop.values())
    devices_per_area: Counter = Counter(ctx.area_of_device(d) for d in ctx.devices)
    names = [a for a in AREA_ORDER if pops_per_area[a]] + sorted(set(pops_per_area) - set(AREA_ORDER))
    nodes = []
    for name in names:
        pos = area_position(name)
        nodes.append({
            "id": f"area:{name}", "kind": "area", "label": name, "pops": pops_per_area[name],
            "devices": devices_per_area[name], "internal": inner[name].as_dict(),
            "lon": pos[0] if pos else None, "lat": pos[1] if pos else None,
        })
    return {"level": "area", "nodes": nodes, "edges": _edges(trunks)}


def _pop_level(ctx: _Ctx, links, area: str) -> dict:
    in_area = {pid for pid, a in ctx.area_of_pop.items() if a == area}
    if not in_area:
        raise HTTPException(404, f"No POPs in area '{area}'")
    inner: dict[int, _Agg] = defaultdict(_Agg)
    edges: dict[tuple[str, str], _Agg] = defaultdict(_Agg)
    external: Counter = Counter()
    for link in links:
        if link.a_device_id not in ctx.devices or link.b_device_id not in ctx.devices:
            continue
        pa, pb = ctx.devices[link.a_device_id].pop_id, ctx.devices[link.b_device_id].pop_id
        a_in, b_in = pa in in_area, pb in in_area
        if not (a_in or b_in):
            continue
        if a_in and b_in:
            if pa == pb:
                inner[pa].add(link)
            else:
                edges[_pair(f"pop:{ctx.pops[pa].code}", f"pop:{ctx.pops[pb].code}")].add(link)
        else:
            local, remote = (pa, pb) if a_in else (pb, pa)
            remote_area = ctx.area_of_pop.get(remote, UNMAPPED_AREA)
            edges[_pair(f"pop:{ctx.pops[local].code}", f"area:{remote_area}")].add(link)
            external[remote_area] += 1

    devices_per_pop: dict[int, list[Any]] = defaultdict(list)
    for d in ctx.devices.values():
        if d.pop_id in in_area:
            devices_per_pop[d.pop_id].append(d)
    nodes = []
    for pid in sorted(in_area, key=lambda p: ctx.pops[p].code):
        p = ctx.pops[pid]
        devs = devices_per_pop[pid]
        nodes.append({
            "id": f"pop:{p.code}", "kind": "pop", "label": p.code, "name": p.name, "area": area,
            "devices": len(devs), "sites": len({site_for(d.hostname, p.code) for d in devs}),
            "categories": dict(Counter(category_for(d.role) for d in devs)),
            "internal": inner[pid].as_dict(), "lat": p.latitude, "lon": p.longitude,
        })
    for remote_area, count in external.items():
        nodes.append({"id": f"area:{remote_area}", "kind": "external_area", "label": remote_area, "links": count})
    return {"level": "pop", "area": area, "nodes": nodes, "edges": _edges(edges)}


def _device_level(ctx: _Ctx, links, pop_code: str) -> dict:
    pop = next((p for p in ctx.pops.values() if p.code.upper() == pop_code.upper()), None)
    if pop is None:
        raise HTTPException(404, f"POP '{pop_code}' not found")
    local = {d.id: d for d in ctx.devices.values() if d.pop_id == pop.id}
    edges: dict[tuple[str, str], _Agg] = defaultdict(_Agg)
    external: dict[str, set[int]] = defaultdict(set)
    for link in links:
        a_in, b_in = link.a_device_id in local, link.b_device_id in local
        if not (a_in or b_in) or link.a_device_id not in ctx.devices or link.b_device_id not in ctx.devices:
            continue
        if a_in and b_in:
            edges[_pair(f"dev:{link.a_device_id}", f"dev:{link.b_device_id}")].add(link)
        else:
            mine, theirs = (link.a_device_id, link.b_device_id) if a_in else (link.b_device_id, link.a_device_id)
            remote_pop = ctx.pop_code(theirs)
            edges[_pair(f"dev:{mine}", f"pop:{remote_pop}")].add(link)
            external[remote_pop].add(theirs)

    nodes = []
    sites = sorted({site_for(d.hostname, pop.code) for d in local.values()})
    for site in sites:
        nodes.append({"id": f"site:{site}", "kind": "site", "label": site if site != "-" else "(tanpa site)"})
    for d in sorted(local.values(), key=lambda d: (ROLE_TIER.get(d.role, 4), d.hostname)):
        nodes.append({
            "id": f"dev:{d.id}", "kind": "device", "device_id": d.id, "label": d.hostname,
            "site": f"site:{site_for(d.hostname, pop.code)}", "role": d.role, "category": category_for(d.role),
            "tier": ROLE_TIER.get(d.role, 4), "vendor": ctx.vendors.get(d.vendor_id or -1), "status": d.status,
            "x": d.map_x, "y": d.map_y,
        })
    for code, remote_devices in sorted(external.items()):
        rp = next((p for p in ctx.pops.values() if p.code == code), None)
        nodes.append({
            "id": f"pop:{code}", "kind": "external_pop", "label": code,
            "area": ctx.area_of_pop.get(rp.id, UNMAPPED_AREA) if rp else UNMAPPED_AREA,
            "devices": len(remote_devices),
        })
    return {"level": "device", "pop": pop.code, "pop_name": pop.name,
            "area": ctx.area_of_pop.get(pop.id, UNMAPPED_AREA), "nodes": nodes, "edges": _edges(edges)}


# --------------------------------------------------------------------------- port capacity


def _port_state(admin: str | None, oper: str | None) -> str:
    if (admin or "").lower() == "down":
        return "disabled"
    return "used" if (oper or "").lower() == "up" else "idle"


@dataclass
class _Usage:
    used: int = 0
    idle: int = 0
    disabled: int = 0
    by_speed: dict[str, Counter] = field(default_factory=lambda: defaultdict(Counter))

    def add(self, bucket: str, state: str, n: int) -> None:
        setattr(self, state, getattr(self, state) + n)
        self.by_speed[bucket][state] += n

    @property
    def total(self) -> int:
        return self.used + self.idle + self.disabled

    def summary(self) -> dict[str, Any]:
        return {
            "total": self.total, "used": self.used, "idle": self.idle, "disabled": self.disabled,
            "used_pct": round(self.used / self.total * 100, 1) if self.total else 0.0,
        }

    def speeds(self) -> list[dict[str, Any]]:
        rows = []
        for bucket in sorted(self.by_speed, key=bucket_sort_key):
            c = self.by_speed[bucket]
            total = c["used"] + c["idle"] + c["disabled"]
            rows.append({"speed": bucket, "total": total, "used": c["used"], "idle": c["idle"],
                         "disabled": c["disabled"], "used_pct": round(c["used"] / total * 100, 1) if total else 0.0})
        return rows


def _port_rows(db: Session):
    """Physical, non-member-agnostic ports grouped per device/speed/state in SQL."""
    return db.execute(
        select(m.Interface.device_id, m.Interface.speed_gbps, m.Interface.admin_status, m.Interface.oper_status,
               func.count())
        .where(m.Interface.if_type == "physical")
        .group_by(m.Interface.device_id, m.Interface.speed_gbps, m.Interface.admin_status, m.Interface.oper_status)
    ).all()


def _device_matches(ctx: _Ctx, device_id: int, category, area, pop, vendor) -> bool:
    d = ctx.devices.get(device_id)
    if d is None:
        return False
    if category and category_for(d.role) != category:
        return False
    if area and ctx.area_of_device(device_id) != area:
        return False
    if pop and ctx.pop_code(device_id).upper() != pop.upper():
        return False
    if vendor and (ctx.vendors.get(d.vendor_id or -1) or "").lower() != vendor.lower():
        return False
    return True


@router.get("/inventory/ports/summary")
def port_summary(
    category: str | None = Query(None, pattern="^(router|metro|olt|access|other)$"),
    area: str | None = None,
    pop: str | None = None,
    vendor: str | None = None,
    db: Session = Depends(get_db),
):
    """Physical ports by speed: used (oper up) / idle (admin up, oper down) / disabled (admin down)."""
    ctx = _context(db)
    overall = _Usage()
    per_category: dict[str, _Usage] = defaultdict(_Usage)
    devices_per_category: dict[str, set[int]] = defaultdict(set)
    for device_id, speed, admin, oper, n in _port_rows(db):
        if not _device_matches(ctx, device_id, category, area, pop, vendor):
            continue
        bucket, state = speed_bucket(speed), _port_state(admin, oper)
        cat = category_for(ctx.devices[device_id].role)
        overall.add(bucket, state, n)
        per_category[cat].add(bucket, state, n)
        devices_per_category[cat].add(device_id)
    order = ["router", "metro", "olt", "access", "other"]
    return {
        "filters": {"category": category, "area": area, "pop": pop, "vendor": vendor},
        "totals": overall.summary(),
        "by_speed": overall.speeds(),
        "by_category": [
            {"category": c, "label": CATEGORY_LABELS[c], "devices": len(devices_per_category[c]),
             **per_category[c].summary(), "by_speed": per_category[c].speeds()}
            for c in order if c in per_category
        ],
        "categories": CATEGORY_LABELS,
        "vendors": sorted({ctx.vendors[v.vendor_id] for v in ctx.devices.values() if v.vendor_id in ctx.vendors}),
    }


@router.get("/inventory/ports/devices")
def port_devices(
    category: str | None = Query(None, pattern="^(router|metro|olt|access|other)$"),
    area: str | None = None,
    pop: str | None = None,
    vendor: str | None = None,
    speed: str | None = None,
    q: str | None = None,
    sort: str = Query("idle", pattern="^(idle|used|total|used_pct|hostname)$"),
    limit: int = Query(200, le=5000),
    db: Session = Depends(get_db),
):
    """Per-device port usage (optionally for one speed bucket), most idle ports first by default."""
    ctx = _context(db)
    usage: dict[int, _Usage] = defaultdict(_Usage)
    for device_id, spd, admin, oper, n in _port_rows(db):
        if not _device_matches(ctx, device_id, category, area, pop, vendor):
            continue
        bucket = speed_bucket(spd)
        if speed and bucket != speed:
            continue
        usage[device_id].add(bucket, _port_state(admin, oper), n)
    rows = []
    needle = (q or "").lower()
    for device_id, u in usage.items():
        d = ctx.devices[device_id]
        if needle and needle not in d.hostname.lower():
            continue
        rows.append({
            "device_id": device_id, "hostname": d.hostname, "pop": ctx.pop_code(device_id),
            "area": ctx.area_of_device(device_id), "site": site_for(d.hostname, ctx.pop_code(device_id)),
            "role": d.role, "category": category_for(d.role), "vendor": ctx.vendors.get(d.vendor_id or -1),
            **u.summary(),
            "speeds": {r["speed"]: {"used": r["used"], "idle": r["idle"], "disabled": r["disabled"]} for r in u.speeds()},
        })
    reverse = sort != "hostname"
    rows.sort(key=lambda r: r[sort] if sort != "hostname" else r["hostname"], reverse=reverse)
    return {"total_devices": len(rows), "devices": rows[:limit]}
