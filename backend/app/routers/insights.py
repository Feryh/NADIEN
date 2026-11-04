"""Advanced dashboard (Phase 8): network & area trends with forecast, services & validation, data health, area map.

Everything is computed from the existing engines (capacity forecast, hotspots, validation) and cached like the
other read models; nothing here writes.
"""

from collections import Counter, defaultdict
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import models as m
from ..config import settings
from ..database import get_db
from ..services import cache
from ..services.areas import AREA_ORDER, UNMAPPED_AREA, area_for_pop, area_position
from ..services.capacity import hotspot_level
from .capacity import _Node, _context, _daily_peaks, _link_rows, all_link_capacity
from .validation import _all as validation_rows

router = APIRouter(prefix="/dashboard", tags=["Dashboard"])

ROUTER_ROLES = ("p", "pe", "agg", "bng", "dci")  # devices expected to carry a loopback and IS-IS
COLLECTOR_KINDS = ("observium", "rrd", "gitweb")
LEVEL_RANK = {"critical": 3, "warning": 2, "normal": 1, "suspect": 0}


def _pop_areas(db: Session) -> dict[int, str]:
    return {p.id: area_for_pop(p.code, p.region, p.name) for p in db.execute(select(m.Pop.id, m.Pop.code, m.Pop.region,
                                                                                    m.Pop.name))}


# --------------------------------------------------------------------------- trends & forecast


@router.get("/trends")
def trends(db: Session = Depends(get_db)):
    """Daily peak traffic of the whole network and of each area, with the capacity forecast of each."""
    return cache.cached(("dashboard", "trends"), lambda: _trends(db))


def _trends(db: Session) -> dict[str, Any]:
    devices, _ = _context(db)
    area_of_pop = _pop_areas(db)
    peaks = _daily_peaks(db)
    network = _Node("network", "Seluruh jaringan", None)
    areas: dict[str, _Node] = {}
    for link in _link_rows(db):
        link_peaks = peaks.get(link.id, {})
        network.add(link, link_peaks)
        for area in {area_of_pop.get(devices[d].pop_id, UNMAPPED_AREA) for d in (link.a_device_id, link.b_device_id)
                     if d in devices}:
            areas.setdefault(area, _Node(f"area:{area}", area, None)).add(link, link_peaks)
    rows = [_with_series(node) for node in areas.values() if node.capacity > 0]
    rows.sort(key=lambda r: AREA_ORDER.index(r["label"]) if r["label"] in AREA_ORDER else len(AREA_ORDER))
    return {"network": _with_series(network), "areas": rows, "window_days": settings.trend_window_days}


def _with_series(node: _Node) -> dict[str, Any]:
    days = sorted(node.daily)[-settings.trend_window_days:]
    return {**node.forecast(), "series": [{"day": d, "peak_gbps": round(node.daily[d], 2)} for d in days]}


# --------------------------------------------------------------------------- services & validation


@router.get("/services")
def services(db: Session = Depends(get_db)):
    """Services by type and status, validation results and MoPs that would still be DRAFT."""
    return cache.cached(("dashboard", "services"), lambda: _services(db))


def _services(db: Session) -> dict[str, Any]:
    rows = db.execute(select(m.Service.service_type, m.Service.status, m.Service.bandwidth_mbps)).all()
    live = [r for r in rows if r.status != "terminated"]
    validated = cache.cached(("validation", "all"), lambda: validation_rows(db))
    findings = Counter(title for r in validated for title in r["failed"])
    return {
        "total": len(live),
        "by_type": dict(Counter(r.service_type for r in live)),
        "by_status": dict(Counter(r.status for r in rows)),
        "bandwidth_by_type_mbps": {t: sum(r.bandwidth_mbps for r in live if r.service_type == t)
                                   for t in {r.service_type for r in live}},
        "validation": {s: sum(1 for r in validated if r["status"] == s) for s in ("fail", "warn", "pass")},
        "draft_mops": [{"service_id": r["service_id"], "circuit_id": r["circuit_id"], "service_type": r["service_type"],
                        "failed": r["failed"]} for r in validated if r["status"] == "fail"][:50],
        "top_findings": [{"title": t, "services": n} for t, n in findings.most_common(8)],
    }


# --------------------------------------------------------------------------- data health


@router.get("/data-health")
def data_health(db: Session = Depends(get_db)):
    """Gaps in the inventory that weaken topology, forecast and validation, and how fresh each sync is."""
    return cache.cached(("dashboard", "data-health"), lambda: _data_health(db))


def _check(key: str, label: str, count: int, total: int, severity: str, hint: str) -> dict[str, Any]:
    return {"key": key, "label": label, "count": count, "total": total, "severity": severity if count else "ok",
            "hint": hint}


def _data_health(db: Session) -> dict[str, Any]:
    pops = db.execute(select(m.Pop.id, m.Pop.code, m.Pop.region, m.Pop.name, m.Pop.latitude)).all()
    pops_with_devices = set(db.scalars(select(m.Device.pop_id).distinct()))
    used_pops = [p for p in pops if p.id in pops_with_devices]
    located_pops = {p.id for p in pops if p.latitude is not None}
    devices = db.execute(select(m.Device.id, m.Device.pop_id, m.Device.role, m.Device.loopback_ipv4,
                                m.Device.isis_net)).all()
    own_location = set(db.scalars(select(m.DeviceLocation.device_id)))
    routers = [d for d in devices if d.role in ROUTER_ROLES]
    links = all_link_capacity(db)
    sources = Counter(db.scalars(select(m.Link.discovery_source)))
    link_ifaces = {i for row in db.execute(select(m.Link.a_interface_id, m.Link.b_interface_id)) for i in row if i}
    no_mtu = db.scalar(select(func.count()).select_from(m.Interface).where(
        m.Interface.id.in_(link_ifaces), m.Interface.mtu.is_(None))) if link_ifaces else 0
    history_days = db.scalar(select(func.count(func.distinct(func.date(m.UtilizationSample.ts))))) or 0
    checks = [
        _check("pops_without_coordinates", "POP tanpa koordinat", sum(1 for p in used_pops if p.id not in located_pops),
               len(used_pops), "warn", "Import KMZ/KML di Topology agar POP tampil di peta."),
        _check("pops_without_area", "POP belum masuk area",
               sum(1 for p in used_pops if area_for_pop(p.code, p.region, p.name) == UNMAPPED_AREA), len(used_pops),
               "warn", "Isi area (region) POP di Inventory → POP."),
        _check("devices_without_location", "Device tanpa lokasi (site maupun POP)",
               sum(1 for d in devices if d.id not in own_location and d.pop_id not in located_pops), len(devices),
               "info", "Tampil di peta hanya bila POP atau site-nya punya koordinat."),
        _check("routers_without_loopback", "Router tanpa loopback", sum(1 for d in routers if not d.loopback_ipv4),
               len(routers), "warn", "Dibutuhkan untuk pseudowire dan validasi layanan."),
        _check("routers_without_isis", "Router tanpa IS-IS NET", sum(1 for d in routers if not d.isis_net), len(routers),
               "warn", "Muncul dari parser config (RANCID/gitweb); cek perangkat yang config-nya belum ter-sync."),
        _check("links_without_lldp", "Link tanpa konfirmasi LLDP", len(links) - sources.get("lldp", 0), len(links),
               "info", "Link hanya dari deskripsi config/manual; LLDP adalah sumber utama topologi."),
        _check("links_without_capacity", "Link tanpa kapasitas", sum(1 for l in links if l.capacity_gbps <= 0),
               len(links), "warn", "Kapasitas 0 membuat utilisasi dan forecast tidak bisa dihitung."),
        _check("links_suspect", "Link dengan data utilisasi janggal",
               sum(1 for l in links if l.capacity_gbps > 0 and hotspot_level(l.utilization_pct) == "suspect"),
               len(links), "warn", "Utilisasi di atas batas wajar: kapasitas atau counter kemungkinan salah."),
        _check("link_ports_without_mtu", "Port link tanpa data MTU", no_mtu or 0, len(link_ifaces), "info",
               "Validasi MTU jalur butuh data ini (sync config RANCID)."),
        _check("utilization_history", "Histori utilisasi (hari)", max(settings.trend_window_days - history_days, 0),
               settings.trend_window_days, "warn" if history_days < 30 else "info",
               f"Baru {history_days} hari tersedia; forecast butuh ≥ 30 hari (import RRD Observium)."),
    ]
    return {"checks": checks, "syncs": _syncs(db)}


def _syncs(db: Session) -> list[dict[str, Any]]:
    now = datetime.now(timezone.utc)
    jobs = db.execute(select(m.CollectorJob.kind, m.CollectorJob.status, m.CollectorJob.finished_at,
                             m.CollectorJob.error).where(m.CollectorJob.finished_at.is_not(None))
                      .order_by(m.CollectorJob.finished_at.desc())).all()
    latest: dict[str, dict[str, Any]] = {}
    for job in jobs:
        kind = next((k for k in COLLECTOR_KINDS if job.kind.startswith(k)), job.kind)
        entry = latest.setdefault(kind, {"kind": kind, "last_ok": None, "last_failed": None, "error": None})
        finished = job.finished_at if job.finished_at.tzinfo else job.finished_at.replace(tzinfo=timezone.utc)
        if job.status == "ok" and entry["last_ok"] is None:
            entry["last_ok"] = finished.isoformat()
            entry["age_hours"] = round((now - finished).total_seconds() / 3600, 1)
        elif job.status == "failed" and entry["last_failed"] is None:
            entry["last_failed"], entry["error"] = finished.isoformat(), (job.error or "")[:200]
    return [latest.get(k, {"kind": k, "last_ok": None, "last_failed": None, "error": None}) for k in COLLECTOR_KINDS] + \
        [v for k, v in latest.items() if k not in COLLECTOR_KINDS]


# --------------------------------------------------------------------------- area map


@router.get("/areas")
def areas(db: Session = Depends(get_db)):
    """Per area: position (POP centroid, else the area's approximate centre), capacity and worst hotspot level;
    plus every located POP with its own worst level, for the hotspot map."""
    return cache.cached(("dashboard", "areas"), lambda: _areas(db))


def _areas(db: Session) -> dict[str, Any]:
    pops = db.execute(select(m.Pop.id, m.Pop.code, m.Pop.name, m.Pop.region, m.Pop.latitude, m.Pop.longitude)).all()
    area_of_pop = {p.id: area_for_pop(p.code, p.region, p.name) for p in pops}
    devices, _ = _context(db)
    pop_of_host = {d.hostname: d.pop_id for d in devices.values()}
    totals: dict[str, dict[str, Any]] = defaultdict(lambda: {"capacity_gbps": 0.0, "current_gbps": 0.0,
                                                            "reserved_gbps": 0.0, "links": 0, "critical": 0,
                                                            "warning": 0, "max_utilization_pct": 0.0})
    pop_level: dict[int, tuple[str, float]] = {}
    for link in all_link_capacity(db):
        if link.capacity_gbps <= 0:
            continue
        level = hotspot_level(link.utilization_pct)
        ends = [pop_of_host[h] for h in (link.a_device, link.b_device) if h in pop_of_host]
        for pop_id in set(ends):
            worst = pop_level.get(pop_id, ("normal", 0.0))
            if LEVEL_RANK[level] > LEVEL_RANK[worst[0]] or (level == worst[0] and link.utilization_pct > worst[1]):
                pop_level[pop_id] = (level, link.utilization_pct)
        for area in {area_of_pop.get(p, UNMAPPED_AREA) for p in ends}:
            t = totals[area]
            t["capacity_gbps"] += link.capacity_gbps
            t["current_gbps"] += link.current_gbps
            t["reserved_gbps"] += link.reserved_gbps
            t["links"] += 1
            if level in ("critical", "warning"):
                t[level] += 1
                t["max_utilization_pct"] = max(t["max_utilization_pct"], link.utilization_pct)
    located: dict[str, list[tuple[float, float]]] = defaultdict(list)
    for p in pops:
        if p.latitude is not None and p.longitude is not None:
            located[area_of_pop[p.id]].append((p.latitude, p.longitude))
    rows = []
    for area in AREA_ORDER:
        t = totals.get(area)
        if t is None:
            continue
        # Unmapped POPs are spread over the whole country: their centroid would mark a meaningless spot.
        points = located.get(area, []) if area != UNMAPPED_AREA else []
        approx = area_position(area)  # (longitude, latitude)
        lat, lon, estimated = ((sum(x for x, _ in points) / len(points), sum(y for _, y in points) / len(points), False)
                               if points else ((approx[1], approx[0], True) if approx else (None, None, True)))
        committed = (t["current_gbps"] + t["reserved_gbps"]) / t["capacity_gbps"] * 100 if t["capacity_gbps"] else 0
        level = "critical" if t["critical"] else "warning" if t["warning"] else "normal"
        rows.append({"area": area, "latitude": lat, "longitude": lon, "position_estimated": estimated,
                     "pops": sum(1 for a in area_of_pop.values() if a == area), "located_pops": len(points),
                     **{k: round(v, 1) if isinstance(v, float) else v for k, v in t.items()},
                     "committed_pct": round(committed, 1), "level": level})
    pop_rows = [{"pop_id": p.id, "code": p.code, "name": p.name, "area": area_of_pop[p.id], "latitude": p.latitude,
                 "longitude": p.longitude, "level": pop_level.get(p.id, ("normal", 0.0))[0],
                 "max_utilization_pct": round(pop_level.get(p.id, ("normal", 0.0))[1], 1)}
                for p in pops if p.latitude is not None and p.longitude is not None and p.id in pop_level]
    return {"areas": rows, "pops": pop_rows,
            "thresholds": {"warning_pct": settings.hotspot_warning_pct, "critical_pct": settings.hotspot_critical_pct}}
