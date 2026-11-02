from collections import defaultdict
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models as m
from .. import schemas as s
from ..database import get_db
from ..services.capacity import calculate, forecast_series, trend_gbps_per_month

router = APIRouter(prefix="/capacity", tags=["Capacity Planning"])

TREND_WINDOW_DAYS = 90


def _link_capacity(link: m.Link, samples, devices, pops) -> s.LinkCapacity:
    if link.growth_gbps_per_month is not None:
        growth, source = link.growth_gbps_per_month, "manual"
    else:
        growth, source = trend_gbps_per_month(samples), "trend"
    result = calculate(
        link.capacity_gbps, link.current_gbps, link.reserved_gbps, growth_abs=growth, growth_source=source
    )
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
    )


def _context(db: Session):
    devices = {d.id: d for d in db.scalars(select(m.Device))}
    pops = {p.id: p.code for p in db.scalars(select(m.Pop))}
    return devices, pops


def _samples_by_link(db: Session, link_id: int | None = None) -> dict[int, list[m.UtilizationSample]]:
    since = datetime.now(timezone.utc) - timedelta(days=TREND_WINDOW_DAYS)
    stmt = select(m.UtilizationSample).where(m.UtilizationSample.ts >= since).order_by(m.UtilizationSample.ts)
    if link_id is not None:
        stmt = stmt.where(m.UtilizationSample.link_id == link_id)
    grouped: dict[int, list[m.UtilizationSample]] = defaultdict(list)
    for sample in db.scalars(stmt):
        grouped[sample.link_id].append(sample)
    return grouped


def all_link_capacity(db: Session) -> list[s.LinkCapacity]:
    devices, pops = _context(db)
    samples = _samples_by_link(db)
    return [_link_capacity(l, samples.get(l.id, []), devices, pops) for l in db.scalars(select(m.Link))]


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
    devices, _ = _context(db)
    totals: dict[int, dict[str, float]] = defaultdict(lambda: {"capacity_gbps": 0.0, "current_gbps": 0.0, "reserved_gbps": 0.0, "links": 0})
    for link in db.scalars(select(m.Link)):
        pop_ids = {devices[d].pop_id for d in (link.a_device_id, link.b_device_id) if d in devices}
        for pop_id in pop_ids:
            t = totals[pop_id]
            t["capacity_gbps"] += link.capacity_gbps
            t["current_gbps"] += link.current_gbps
            t["reserved_gbps"] += link.reserved_gbps
            t["links"] += 1
    rows = []
    for pop in db.scalars(select(m.Pop).order_by(m.Pop.code)):
        t = totals[pop.id]
        result = calculate(t["capacity_gbps"], t["current_gbps"], t["reserved_gbps"])
        rows.append({
            "pop_id": pop.id,
            "code": pop.code,
            "name": pop.name,
            "links": int(t["links"]),
            "devices": sum(1 for d in devices.values() if d.pop_id == pop.id),
            **result.model_dump(include={"capacity_gbps", "current_gbps", "reserved_gbps", "available_gbps", "utilization_pct", "committed_pct", "color"}),
        })
    return rows
