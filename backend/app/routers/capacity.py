from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import models as m
from .. import schemas as s
from ..config import settings
from ..database import get_db
from ..services import cache
from ..services.capacity import (
    MIN_TREND_DAYS,
    calculate,
    fit_trend,
    forecast_series,
    hotspot_level,
    trend_gbps_per_month,
)

router = APIRouter(prefix="/capacity", tags=["Capacity Planning"])

TREND_WINDOWS = (7, 30, 90)


def _window(samples, days: int):
    """Samples of the last `days` days, measured back from the newest sample (no clock/tz mixing)."""
    if not samples:
        return []
    newest = max(s.ts.timestamp() for s in samples)
    return [s for s in samples if s.ts.timestamp() >= newest - days * 86400]


def growth_trends(samples) -> dict[str, float | None]:
    out = {}
    for days in TREND_WINDOWS:
        g = trend_gbps_per_month(_window(samples, days))
        out[str(days)] = round(g, 3) if g is not None else None
    return out


def _forecast_inputs(manual_growth: float | None, samples) -> tuple[float | None, str, Any]:
    """(growth, source, trend) — the trend over NADIEN_TREND_WINDOW_DAYS drives both the growth and,
    with enough history, the level the forecast starts from."""
    fit = fit_trend([(x.ts.timestamp() / 86400, max(x.in_gbps or 0, x.out_gbps or 0))
                     for x in _window(samples, settings.trend_window_days)])
    trend = fit if fit is not None and fit.days >= MIN_TREND_DAYS else None
    if manual_growth is not None:
        return manual_growth, "manual", trend
    return (fit.growth_per_month if fit else None), "trend", trend


def _link_capacity(link, samples, devices, pops) -> s.LinkCapacity:
    growth, source, trend = _forecast_inputs(link.growth_gbps_per_month, samples)
    result = calculate(link.capacity_gbps, link.current_gbps, link.reserved_gbps, growth_abs=growth,
                       growth_source=source, trend=trend)
    a, b = devices.get(link.a_device_id), devices.get(link.b_device_id)
    return s.LinkCapacity(
        **result.model_dump(),
        link_id=link.id,
        name=link.name,
        link_type=link.link_type,
        status=link.status,
        a_device=a.hostname if a else "?",
        b_device=b.hostname if b else "?",
        a_pop=pops.get(a.pop_id) if a else None,
        b_pop=pops.get(b.pop_id) if b else None,
        growth_trends=growth_trends(samples),
        samples_days=_span_days(samples),
    )


def _span_days(samples) -> int:
    if len(samples) < 2:
        return 0
    stamps = [s.ts.timestamp() for s in samples]
    return round((max(stamps) - min(stamps)) / 86400)


def _context(db: Session):
    devices = {d.id: d for d in db.execute(select(m.Device.id, m.Device.hostname, m.Device.pop_id, m.Device.role))}
    pops = {p.id: p.code for p in db.execute(select(m.Pop.id, m.Pop.code))}
    return devices, pops


def _since() -> datetime:
    return datetime.now(timezone.utc) - timedelta(days=max(max(TREND_WINDOWS), settings.trend_window_days) + 1)


def _samples_by_link(db: Session, link_id: int | None = None) -> dict[int, list[Any]]:
    """Plain rows (link_id, ts, in, out), not ORM objects: 9k links × 90 days is ~1M samples."""
    cols = select(m.UtilizationSample.link_id, m.UtilizationSample.ts, m.UtilizationSample.in_gbps,
                  m.UtilizationSample.out_gbps)
    stmt = cols.where(m.UtilizationSample.ts >= _since()).order_by(m.UtilizationSample.ts)
    if link_id is not None:
        stmt = stmt.where(m.UtilizationSample.link_id == link_id)
    grouped: dict[int, list[Any]] = defaultdict(list)
    for row in db.execute(stmt):
        grouped[row.link_id].append(row)
    return grouped


def _link_rows(db: Session):
    return db.execute(select(m.Link.id, m.Link.name, m.Link.link_type, m.Link.status, m.Link.a_device_id,
                             m.Link.b_device_id, m.Link.capacity_gbps, m.Link.current_gbps, m.Link.reserved_gbps,
                             m.Link.growth_gbps_per_month)).all()


def all_link_capacity(db: Session) -> list[s.LinkCapacity]:
    def compute() -> list[s.LinkCapacity]:
        devices, pops = _context(db)
        samples = _samples_by_link(db)
        return [_link_capacity(l, samples.get(l.id, []), devices, pops) for l in _link_rows(db)]

    return cache.cached(("capacity", "links"), compute)


@router.post("/calculate", response_model=s.CapacityResult)
def what_if(payload: s.CapacityCalcIn):
    """Stateless what-if calculator, e.g. 400G / 250G / 20G per month -> full in 7.5 months."""
    return calculate(
        payload.capacity_gbps,
        payload.current_gbps,
        payload.reserved_gbps,
        growth_abs=payload.growth_gbps_per_month if payload.growth_pct_per_month is None else None,
        growth_pct=payload.growth_pct_per_month,
        growth_source="input",
        threshold_pct=payload.threshold_pct,
    )


@router.get("/links", response_model=list[s.LinkCapacity])
def list_link_capacity(color: str | None = Query(None, pattern="^(green|yellow|red)$"), db: Session = Depends(get_db)):
    """All links with utilization and exhaustion forecast, soonest-to-exhaust first."""
    rows = all_link_capacity(db)
    if color:
        rows = [r for r in rows if r.color == color]
    return sorted(rows, key=lambda r: (r.months_to_threshold is None, r.months_to_threshold or 0, -r.committed_pct))


@router.get("/links/{link_id}", response_model=s.LinkCapacityDetail)
def link_capacity_detail(link_id: int, months: int = Query(24, ge=1, le=120), db: Session = Depends(get_db)):
    link = db.get(m.Link, link_id)
    if link is None:
        raise HTTPException(404, "Link not found")
    devices, pops = _context(db)
    samples = _samples_by_link(db, link_id).get(link_id, [])
    result = _link_capacity(link, samples, devices, pops)
    return s.LinkCapacityDetail(
        **result.model_dump(),
        history=[s.SampleIn(link_id=x.link_id, ts=x.ts, in_gbps=x.in_gbps, out_gbps=x.out_gbps) for x in samples],
        forecast=forecast_series(result, months),
    )


@router.post("/samples")
def ingest_samples(samples: list[s.SampleIn], db: Session = Depends(get_db)):
    """Bulk-load utilization samples (from gNMI/Telegraf/NMS). Updates each link's current_gbps to its latest peak."""
    latest: dict[int, s.SampleIn] = {}
    for sample in samples:
        if db.get(m.Link, sample.link_id) is None:
            raise HTTPException(404, f"Link {sample.link_id} not found")
        db.add(m.UtilizationSample(**sample.model_dump()))
        if sample.link_id not in latest or sample.ts > latest[sample.link_id].ts:
            latest[sample.link_id] = sample
    for link_id, sample in latest.items():
        db.get(m.Link, link_id).current_gbps = max(sample.in_gbps, sample.out_gbps)
    db.commit()
    return {"stored": len(samples), "links_updated": len(latest)}


@router.get("/pops")
def pop_utilization(db: Session = Depends(get_db)):
    """Aggregate capacity of all links touching each POP (inter-POP links count for both ends)."""
    return cache.cached(("capacity", "pops"), lambda: _pop_utilization(db))


def _pop_utilization(db: Session) -> list[dict[str, Any]]:
    devices, _ = _context(db)
    totals: dict[int, dict[str, float]] = defaultdict(lambda: {"capacity_gbps": 0.0, "current_gbps": 0.0, "reserved_gbps": 0.0, "links": 0})
    for link in _link_rows(db):
        pop_ids = {devices[d].pop_id for d in (link.a_device_id, link.b_device_id) if d in devices}
        for pop_id in pop_ids:
            t = totals[pop_id]
            t["capacity_gbps"] += link.capacity_gbps
            t["current_gbps"] += link.current_gbps
            t["reserved_gbps"] += link.reserved_gbps
            t["links"] += 1
    devices_per_pop: dict[int, int] = defaultdict(int)
    for d in devices.values():
        devices_per_pop[d.pop_id] += 1
    rows = []
    for pop in db.scalars(select(m.Pop).order_by(m.Pop.code)):
        t = totals[pop.id]
        result = calculate(t["capacity_gbps"], t["current_gbps"], t["reserved_gbps"])
        rows.append({
            "pop_id": pop.id,
            "code": pop.code,
            "name": pop.name,
            "links": int(t["links"]),
            "devices": devices_per_pop[pop.id],
            **result.model_dump(include={"capacity_gbps", "current_gbps", "reserved_gbps", "available_gbps", "utilization_pct", "committed_pct", "color"}),
        })
    return rows


# --------------------------------------------------------------------------- node forecast


def _daily_peaks(db: Session) -> dict[int, dict[str, float]]:
    """link_id -> {day: peak Gbps}. Per-day max of in and of out equals the day's max of max(in, out)."""
    day = func.date(m.UtilizationSample.ts)
    stmt = (select(m.UtilizationSample.link_id, day.label("day"), func.max(m.UtilizationSample.in_gbps),
                   func.max(m.UtilizationSample.out_gbps))
            .where(m.UtilizationSample.ts >= _since())
            .group_by(m.UtilizationSample.link_id, day))
    peaks: dict[int, dict[str, float]] = defaultdict(dict)
    for link_id, d, peak_in, peak_out in db.execute(stmt):
        peaks[link_id][str(d)] = max(peak_in or 0, peak_out or 0)
    return peaks


class _Node:
    def __init__(self, key: str, label: str, pop: str | None):
        self.key, self.label, self.pop = key, label, pop
        self.links: set[int] = set()
        self.capacity = self.current = self.reserved = 0.0
        self.daily: dict[str, float] = defaultdict(float)

    def add(self, link, peaks: dict[str, float]) -> None:
        if link.id in self.links:
            return
        self.links.add(link.id)
        self.capacity += link.capacity_gbps or 0
        self.current += link.current_gbps or 0
        self.reserved += link.reserved_gbps or 0
        for d, peak in peaks.items():
            self.daily[d] += peak

    def forecast(self) -> dict[str, Any]:
        days = sorted(self.daily)
        points = [(datetime.fromisoformat(d).timestamp() / 86400, self.daily[d]) for d in days]
        points = [p for p in points if p[0] >= (points[-1][0] - settings.trend_window_days)] if points else []
        fit = fit_trend(points)
        trend = fit if fit is not None and fit.days >= MIN_TREND_DAYS else None
        result = calculate(self.capacity, self.current, self.reserved, growth_abs=fit.growth_per_month if fit else None,
                           growth_source="trend", trend=trend)
        return {"key": self.key, "label": self.label, "pop": self.pop, "links": len(self.links),
                **result.model_dump(), "history_days": len(days)}


@router.get("/nodes")
def node_forecast(
    level: str = Query("device", pattern="^(device|pop)$"),
    q: str | None = None,
    sort: str = Query("months_to_threshold", pattern="^(months_to_threshold|utilization_pct|growth|capacity)$"),
    limit: int = Query(200, ge=1, le=5000),
    db: Session = Depends(get_db),
):
    """Forecast per device or per POP: total daily peak traffic of all its links over the last
    NADIEN_TREND_WINDOW_DAYS days, least-squares trend, months until the threshold/full."""
    rows = cache.cached(("capacity", "nodes", level), lambda: _node_rows(db, level))
    needle = (q or "").strip().lower()
    if needle:
        rows = [r for r in rows if needle in r["label"].lower() or needle in (r["pop"] or "").lower()]
    keys = {
        "months_to_threshold": lambda r: (r["months_to_threshold"] is None, r["months_to_threshold"] or 0, -r["utilization_pct"]),
        "utilization_pct": lambda r: -r["utilization_pct"],
        "growth": lambda r: -(r["growth_gbps_per_month"] or 0),
        "capacity": lambda r: -r["capacity_gbps"],
    }
    return {"level": level, "total": len(rows), "nodes": sorted(rows, key=keys[sort])[:limit]}


def _node_rows(db: Session, level: str) -> list[dict[str, Any]]:
    devices, pops = _context(db)
    peaks = _daily_peaks(db)
    nodes: dict[int, _Node] = {}
    for link in _link_rows(db):
        ends = [devices.get(link.a_device_id), devices.get(link.b_device_id)]
        for d in {e for e in ends if e is not None}:
            key = d.id if level == "device" else d.pop_id
            if key not in nodes:
                label = d.hostname if level == "device" else pops.get(d.pop_id, "?")
                nodes[key] = _Node(f"{level}:{key}", label, pops.get(d.pop_id))
            nodes[key].add(link, peaks.get(link.id, {}))
    return [n.forecast() for n in nodes.values() if n.capacity > 0]


# --------------------------------------------------------------------------- hotspots


@router.get("/hotspots")
def hotspots(limit: int = Query(100, ge=1, le=2000), db: Session = Depends(get_db)):
    """Busiest links by utilization (warning ≥ 70%, critical ≥ 90%) and devices by port occupancy."""
    from .analysis import device_port_usage  # late import: analysis is the heavier module

    links = sorted(all_link_capacity(db), key=lambda l: -l.utilization_pct)
    link_rows = [{"link_id": l.link_id, "a_device": l.a_device, "b_device": l.b_device, "a_pop": l.a_pop,
                  "b_pop": l.b_pop, "status": l.status, "capacity_gbps": l.capacity_gbps, "current_gbps": l.current_gbps,
                  "utilization_pct": l.utilization_pct, "level": hotspot_level(l.utilization_pct),
                  "months_to_threshold": l.months_to_threshold} for l in links if l.capacity_gbps > 0]
    devices = sorted(device_port_usage(db), key=lambda d: (-d["used_pct"], -d["used"]))
    device_rows = [{**d, "level": hotspot_level(d["used_pct"])} for d in devices if d["total"] > 0]

    def counts(rows: list[dict[str, Any]]) -> dict[str, int]:
        return {lvl: sum(1 for r in rows if r["level"] == lvl) for lvl in ("critical", "warning", "normal", "suspect")}

    # Real hotspots first (critical/warning by utilization), suspect capacity data after them.
    link_rows.sort(key=lambda r: (r["level"] == "suspect", -r["utilization_pct"]))
    return {
        "thresholds": {"warning_pct": settings.hotspot_warning_pct, "critical_pct": settings.hotspot_critical_pct,
                       "suspect_pct": settings.hotspot_suspect_pct},
        "links": {"counts": counts(link_rows), "rows": link_rows[:limit]},
        "devices": {"counts": counts(device_rows), "rows": device_rows[:limit]},
    }
