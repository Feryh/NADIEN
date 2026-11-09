"""Read models for a large network: areas, a drill-down topology, and port capacity usage.

Everything is aggregated per request from devices/links/interfaces (thousands of devices,
hundreds of thousands of ports); interfaces are grouped in SQL first.
"""

import math
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Any

import zipfile

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from pydantic import BaseModel, Field

from .. import models as m
from ..collectors import runner as jobs_runner
from ..collectors.jobs import JobError, backup_before
from ..database import get_db
from ..services import cache
from ..services.areas import (
    AREA_ORDER,
    AREAS,
    CATEGORY_LABELS,
    CATEGORY_ORDER,
    CATEGORY_PATTERN,
    ROLE_TIER,
    UNMAPPED_AREA,
    area_for_hostnames,
    area_for_name,
    area_for_pop,
    city_center_for_code,
    city_for_pop,
    city_label,
    csr_parts,
    first_city_code,
    is_city_code,
    pop_code_for_hostname,
    sibling_codes,
    area_position,
    bucket_sort_key,
    category_for,
    site_for,
    speed_bucket,
)
from ..services.capacity import occupancy_pct
from ..services.topology_readmodel import (
    UNASSIGNED_POP,
    Agg as _Agg,
    Ctx as _Ctx,
    context as _context,
    device_node as _device_node,
    edges_of as _edges,
    load_links as _links,
    pair as _pair,
    path_fields as _path_fields,
    place_devices as _place_devices,
    pop_labels as _pop_labels,
    segment_of as _segment_of,
    segments_of as _segments,
)
from ..services.geo import KmlError, Placemark, match_devices, match_pops, read_placemarks

router = APIRouter(tags=["Analysis"])

_MISSING_LOCATION_VALUES = {"", "unknown", "n/a", "na", "none", "-", UNMAPPED_AREA.lower()}


def _missing_location(value: str | None) -> bool:
    return (value or "").strip().lower() in _MISSING_LOCATION_VALUES


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
    """1. Devices parked in UNASSIGNED move to the POP their hostname names explicitly
          (city name or city code: PE1-ATN910-KEBUMEN -> KBM); missing POPs are created.
       2. Blank POP regions are filled from the POP code (3-letter city / 2-letter province),
          city words in the POP name, or the location most of its device hostnames agree on.
       Never overwrites a region that is already set."""
    jobs_runner.ensure_idle(jobs_runner.runner)
    pops = {p.code.upper(): p for p in db.scalars(select(m.Pop))}
    unassigned = pops.get(UNASSIGNED_POP)
    moved: Counter = Counter()
    created: list[str] = []
    if unassigned is not None:
        for device in db.scalars(select(m.Device).where(m.Device.pop_id == unassigned.id, m.Device.pop_locked.is_(False))):
            code = pop_code_for_hostname(device.hostname)
            if not code:
                continue
            # Reuse a POP that already exists under another code for the same city (STG vs SLT).
            code = next((c for c in sibling_codes(code) if c in pops), code)
            pop = pops.get(code)
            if pop is None:
                area = area_for_pop(code, None, None)
                pop = m.Pop(code=code, name=city_label(code) or code, status="active",
                            region=None if area == UNMAPPED_AREA else area)
                db.add(pop)
                db.flush()
                pops[code] = pop
                created.append(code)
            device.pop_id = pop.id
            moved[code] += 1
        db.flush()

    assigned: Counter = Counter()
    hostnames_by_pop: dict[int, list[str]] = defaultdict(list)
    for device in db.scalars(select(m.Device)):
        hostnames_by_pop[device.pop_id].append(device.hostname)
    for pop in pops.values():
        region = (pop.region or "").strip()
        if (region and region != UNMAPPED_AREA) or pop.code.upper() == UNASSIGNED_POP:
            continue
        area = area_for_pop(pop.code, None, pop.name)
        if area == UNMAPPED_AREA:
            area = area_for_hostnames(hostnames_by_pop[pop.id])
        if area:
            pop.region = area
            assigned[area] += 1
    db.commit()
    still = [p.code for p in pops.values()
             if not (p.region or "").strip() and p.code.upper() != UNASSIGNED_POP]
    return {
        "assigned": sum(assigned.values()),
        "by_area": dict(assigned),
        "devices_moved": sum(moved.values()),
        "moved_to": dict(moved),
        "pops_created": created,
        "still_unmapped": sorted(still),
    }


@router.post("/pops/rehome")
def rehome_pops(apply: bool = False, db: Session = Depends(get_db)):
    """Review POPs against the hostname rule: a device in a POP whose code is not a registered city
    code (NAP, VRR, CGN, UNASSIGNED, ...) moves to the first registered city code in its hostname
    (CGW-NAP-BDL-KDT -> BDL). POPs left empty are deleted. `apply=false` only previews."""
    backup = None
    if apply:
        jobs_runner.ensure_idle(jobs_runner.runner)
        try:
            backup = backup_before(db, "rehome-pops")  # many devices change POP at once: keep a way back
        except JobError as exc:
            raise HTTPException(500, str(exc)) from exc
    pops = {p.code.upper(): p for p in db.scalars(select(m.Pop))}
    by_id = {p.id: p for p in pops.values()}
    moves: dict[tuple[str, str], list[str]] = defaultdict(list)
    created: list[str] = []
    kept = 0
    for device in db.scalars(select(m.Device)):
        current = by_id.get(device.pop_id)
        if current is None or is_city_code(current.code):
            continue  # registered city POPs are curated as they are
        if device.pop_locked:
            kept += 1  # POP chosen by hand: the rule never overrides it
            continue
        target = _rule_pop(device.hostname)
        if not target or target == current.code.upper():
            continue
        moves[(current.code, target)].append(device.hostname)
        if not apply:
            continue
        pop = pops.get(target)
        if pop is None:
            area = area_for_pop(target, None, None)
            pop = m.Pop(code=target, name=city_label(target) or target, status="active",
                        region=None if area == UNMAPPED_AREA else area)
            db.add(pop)
            db.flush()
            pops[target], by_id[pop.id] = pop, pop
            created.append(target)
        device.pop_id = pop.id
    db.flush()
    sources = {source for source, _ in moves}
    deleted = _delete_empty_pops(db, [pops[s.upper()] for s in sources]) if apply else []
    if apply:
        db.commit()
    remaining = _non_city_pops(db) if apply else None
    return {
        "applied": apply,
        "backup": backup,
        "devices_moved": sum(len(v) for v in moves.values()),
        "moves": [{"from": s, "to": t, "devices": len(h), "examples": h[:5]}
                  for (s, t), h in sorted(moves.items(), key=lambda kv: -len(kv[1]))],
        "pops_created": created,
        "pops_deleted": deleted,
        "non_city_pops_left": remaining,
        "devices_kept_manual": kept,
    }


@router.post("/pops/rehome/keep")
def keep_manual_pop(pop: str, db: Session = Depends(get_db)):
    """"Ikuti manual": lock every device of a POP where it is, so the hostname rule (and sync) stop proposing
    to move it. The POP chosen by hand wins over the city code in the hostname."""
    target = db.scalar(select(m.Pop).where(func.upper(m.Pop.code) == pop.upper()))
    if target is None:
        raise HTTPException(404, f"POP {pop} not found")
    devices = list(db.scalars(select(m.Device).where(m.Device.pop_id == target.id, m.Device.pop_locked.is_(False))))
    for device in devices:
        device.pop_locked = True
    db.commit()
    return {"pop": target.code, "devices_locked": len(devices)}


def _rule_pop(hostname: str) -> str | None:
    parts = csr_parts(hostname)
    return parts[2] if parts else first_city_code(hostname)


def _delete_empty_pops(db: Session, candidates: list[m.Pop]) -> list[str]:
    from .inventory import _POP_REFERENCES  # every column that points at a POP

    deleted = []
    for pop in candidates:
        if pop.code.upper() == UNASSIGNED_POP or is_city_code(pop.code):
            continue
        in_use = any(db.scalar(select(func.count()).select_from(model).where(column == pop.id))
                     for model, column in _POP_REFERENCES)
        if not in_use:
            db.delete(pop)
            deleted.append(pop.code)
    return sorted(deleted)


def _non_city_pops(db: Session) -> list[dict[str, Any]]:
    """Non-city POPs that still hold devices (no registered city code in their hostnames): for manual review."""
    rows = db.execute(select(m.Pop.code, func.count(m.Device.id)).join(m.Device, m.Device.pop_id == m.Pop.id)
                      .group_by(m.Pop.code)).all()
    return [{"code": code, "devices": n} for code, n in sorted(rows, key=lambda r: -r[1])
            if not is_city_code(code) and not _all_cell_sites(db, code)]


def _all_cell_sites(db: Session, code: str) -> bool:
    hostnames = db.scalars(select(m.Device.hostname).join(m.Pop, m.Pop.id == m.Device.pop_id).where(m.Pop.code == code))
    return all(csr_parts(h) for h in hostnames)


MAX_KML_UPLOAD = 30_000_000


@router.post("/geo/placemarks")
async def parse_placemarks(request: Request, filename: str = ""):
    """Read point placemarks from a KML / KMZ / zip body (nothing is stored)."""
    body = await request.body()
    if len(body) > MAX_KML_UPLOAD:
        raise HTTPException(413, "File terlalu besar")
    try:
        placemarks = read_placemarks(body, filename or "upload")
    except (KmlError, zipfile.BadZipFile) as exc:
        raise HTTPException(422, str(exc)) from exc
    return {"source": filename, "placemarks": [p.__dict__ for p in placemarks]}


class PlacemarkIn(BaseModel):
    name: str = Field(max_length=255)
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)
    folder: str | None = None
    source: str | None = None


class CoordinateMatchIn(BaseModel):
    placemarks: list[PlacemarkIn] = Field(max_length=50_000)
    only_missing: bool = True


@router.post("/pops/coordinates/match")
def match_pop_coordinates(payload: CoordinateMatchIn, db: Session = Depends(get_db)):
    """Proposed lat/lon per POP from placemarks (by city, inside the POP's area). Nothing is written."""
    ctx = _context(db)
    pops = [{"id": p.id, "code": p.code, "name": p.name, "city": p.city, "area": ctx.area_of_pop[p.id],
             "latitude": p.latitude, "longitude": p.longitude}
            for p in ctx.pops.values()
            if p.code.upper() != UNASSIGNED_POP and not (payload.only_missing and p.latitude is not None and p.longitude is not None)]
    proposals = match_pops(pops, [Placemark(**p.model_dump()) for p in payload.placemarks])
    matched = {p["pop_id"] for p in proposals}
    return {
        "pops_considered": len(pops),
        "proposals": sorted(proposals, key=lambda p: ({"high": 0, "medium": 1, "low": 2}[p["confidence"]], p["code"])),
        "unmatched": sorted(p["code"] for p in pops if p["id"] not in matched),
    }


class CoordinateIn(BaseModel):
    pop_id: int
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)


@router.put("/pops/coordinates")
def set_pop_coordinates(rows: list[CoordinateIn], db: Session = Depends(get_db)):
    """Write chosen coordinates (backup first)."""
    jobs_runner.ensure_idle(jobs_runner.runner)
    try:
        backup = backup_before(db, "pop-coordinates")
    except JobError as exc:
        raise HTTPException(500, str(exc)) from exc
    updated = 0
    for row in rows:
        pop = db.get(m.Pop, row.pop_id)
        if pop is None:
            raise HTTPException(404, f"POP {row.pop_id} not found")
        pop.latitude, pop.longitude = row.latitude, row.longitude
        updated += 1
    db.commit()
    return {"updated": updated, "backup": backup}


@router.post("/devices/coordinates/match")
def match_device_coordinates(payload: CoordinateMatchIn, db: Session = Depends(get_db)):
    """Proposed site coordinates per device: the hostname's site token (JMB-TLP-OLT1 -> TLP) against
    OLT/ODC placemark names within reach of its POP. Ambiguous abbreviations are skipped. Nothing is written."""
    ctx = _context(db)
    located = set(db.scalars(select(m.DeviceLocation.device_id)))
    devices = []
    for d in ctx.devices.values():
        if payload.only_missing and d.id in located:
            continue
        pop = ctx.pops.get(d.pop_id)
        if pop is None or pop.code.upper() == UNASSIGNED_POP:
            continue
        anchor = (pop.latitude, pop.longitude) if pop.latitude is not None else city_center_for_code(pop.code)
        devices.append({"id": d.id, "hostname": d.hostname, "pop": pop.code, "site": site_for(d.hostname, pop.code),
                        "anchor": anchor})
    proposals, ambiguous = match_devices(devices, [Placemark(**p.model_dump()) for p in payload.placemarks])
    return {
        "devices_considered": len(devices),
        "ambiguous": ambiguous,
        "proposals": sorted(proposals, key=lambda p: (-p["score"], p["pop"], p["hostname"])),
    }


class DeviceCoordinateIn(BaseModel):
    device_id: int
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    placemark: str | None = Field(default=None, max_length=255)
    source: str | None = Field(default=None, max_length=128)


@router.put("/devices/coordinates")
def set_device_coordinates(rows: list[DeviceCoordinateIn], db: Session = Depends(get_db)):
    """Write chosen device site coordinates (backup first)."""
    jobs_runner.ensure_idle(jobs_runner.runner)
    try:
        backup = backup_before(db, "device-coordinates")
    except JobError as exc:
        raise HTTPException(500, str(exc)) from exc
    for row in rows:
        if db.get(m.Device, row.device_id) is None:
            raise HTTPException(404, f"Device {row.device_id} not found")
        location = db.get(m.DeviceLocation, row.device_id) or m.DeviceLocation(device_id=row.device_id)
        location.latitude, location.longitude = row.latitude, row.longitude
        location.placemark, location.source = row.placemark, row.source
        db.add(location)
    db.commit()
    return {"updated": len(rows), "backup": backup}


def _pop_location_mapping(db: Session) -> dict[str, Any]:
    pops = list(db.scalars(select(m.Pop).order_by(m.Pop.code)))
    hostnames_by_pop: dict[int, list[str]] = defaultdict(list)
    for pop_id, hostname in db.execute(select(m.Device.pop_id, m.Device.hostname)):
        hostnames_by_pop[pop_id].append(hostname)

    rows = []
    area_conflicts = []
    for pop in pops:
        if pop.code.upper() == UNASSIGNED_POP:
            continue
        hostnames = hostnames_by_pop[pop.id]
        area = (pop.region or "").strip()
        city = (pop.city or "").strip()
        inferred_area = None
        inferred_city = None
        if _missing_location(area):
            inferred_area = area_for_pop(pop.code, None, pop.name)
            if inferred_area == UNMAPPED_AREA:
                inferred_area = area_for_hostnames(hostnames)
            if inferred_area == UNMAPPED_AREA:
                inferred_area = None
        if _missing_location(city):
            inferred_city = city_for_pop(pop.code, pop.name, hostnames)
        known_area = area if not _missing_location(area) else inferred_area
        known_city = city if not _missing_location(city) else inferred_city
        city_area = area_for_name(known_city)
        if known_area and city_area and known_area != city_area:
            area_conflicts.append({"code": pop.code, "area": known_area, "city": known_city,
                                  "city_area": city_area})
        if inferred_area or inferred_city:
            rows.append({
                "code": pop.code,
                "name": pop.name,
                "area": inferred_area,
                "city": inferred_city,
            })

    mappings_by_code = {row["code"]: row for row in rows}
    area_left = []
    city_left = []
    for pop in pops:
        if pop.code.upper() == UNASSIGNED_POP:
            continue
        planned = mappings_by_code.get(pop.code, {})
        if _missing_location(pop.region) and not planned.get("area"):
            area_left.append(pop.code)
        if _missing_location(pop.city) and not planned.get("city"):
            city_left.append(pop.code)
    return {
        "total_pops": sum(pop.code.upper() != UNASSIGNED_POP for pop in pops),
        "area_updates": sum(bool(row["area"]) for row in rows),
        "city_updates": sum(bool(row["city"]) for row in rows),
        "mappings": rows,
        "area_conflict_count": len(area_conflicts),
        "area_conflicts": area_conflicts,
        "unmapped_area_count": len(area_left),
        "unmapped_area_pops": area_left,
        "unmapped_city_count": len(city_left),
        "unmapped_city_pops": city_left,
    }


@router.get("/areas/pop-location-mapping")
def preview_pop_location_mapping(db: Session = Depends(get_db)):
    """Preview blank POP area/city values inferable from curated codes, names and device hostnames."""
    return _pop_location_mapping(db)


@router.post("/areas/pop-location-mapping")
def apply_pop_location_mapping(db: Session = Depends(get_db)):
    """Fill only blank POP area/city values; leave curated fields and unresolved records untouched."""
    jobs_runner.ensure_idle(jobs_runner.runner)
    result = _pop_location_mapping(db)
    by_code = {pop.code: pop for pop in db.scalars(select(m.Pop))}
    applied_area = applied_city = 0
    for mapping in result["mappings"]:
        pop = by_code[mapping["code"]]
        if mapping["area"] and _missing_location(pop.region):
            pop.region = mapping["area"]
            applied_area += 1
        if mapping["city"] and _missing_location(pop.city):
            pop.city = mapping["city"]
            applied_city += 1
    db.commit()
    remaining = _pop_location_mapping(db)
    return {
        **remaining,
        "applied_area": applied_area,
        "applied_city": applied_city,
    }


# --------------------------------------------------------------------------- drill-down topology


MAX_NEIGHBOR_NODES = 400


@router.get("/topology/view")
def topology_view(
    level: str = Query("area", pattern="^(area|province|regency|pop|device|neighbors)$"),
    area: str | None = None,
    province: str | None = None,
    regency: str | None = None,
    pop: str | None = None,
    device_id: int | None = None,
    depth: int = Query(1, ge=1, le=2),
    db: Session = Depends(get_db),
):
    """area: areas + inter-area trunks · province (needs `area`): provinces of one area ·
    regency (needs `area`, `province`): kabupaten/kota of one province · pop (needs `area`, optionally
    `province` and `regency` to narrow it): POPs + the neighbouring groups they link to ·
    device (needs `pop`): devices of one POP + links out to other POPs ·
    neighbors (needs `device_id`): one device and everything within `depth` hops.
    A province or regency level with a single group inside answers with the next level down
    (the response's `level` says which), so no click opens a view of one box."""
    key = ("topology", level, area, province, regency, (pop or "").upper(), device_id, depth)
    return cache.cached(key, lambda: _with_layout(db, _topology_view(db, level, area, province, regency, pop,
                                                                     device_id, depth)))


def layout_key(view: dict) -> str:
    """Which saved arrangement a view uses: one per area overview, province, regency, POP list, POP and
    neighbourhood (device:JKT, regency:Jawa Tengah & DIY|Jawa Tengah, neighbors:42:1)."""
    level = view["level"]
    if level == "area":
        return "area"
    if level == "device":
        return f"device:{view['pop']}"
    if level == "neighbors":
        return f"neighbors:{view['device_id']}:{view['depth']}"
    return f"{level}:" + "|".join(view.get(k) or "" for k in ("area", "province", "regency"))


def _with_layout(db: Session, view: dict) -> dict:
    view["layout_key"] = layout_key(view)
    row = db.get(m.TopologyLayout, view["layout_key"])
    view["layout"] = row.positions if row else {}
    return view


def _topology_view(db: Session, level: str, area: str | None, province: str | None, regency: str | None,
                   pop: str | None, device_id: int | None, depth: int) -> dict:
    ctx = _context(db)
    links = _links(db)
    if level == "area":
        return _area_level(ctx, links)
    if level in ("province", "regency", "pop"):
        if not area:
            raise HTTPException(422, f"area is required for level={level}")
        if level == "regency" and not province:
            raise HTTPException(422, "province is required for level=regency")
        scope = (area,) if level == "province" else (area, province) if level == "regency" else \
            tuple(v for v in (area, province, regency) if v)
        return _scoped_level(ctx, links, scope, level == "pop")
    if level == "neighbors":
        if device_id is None:
            raise HTTPException(422, "device_id is required for level=neighbors")
        return _neighbor_level(ctx, links, device_id, depth)
    if not pop:
        raise HTTPException(422, "pop is required for level=device")
    return _device_level(ctx, links, pop)


def _neighbor_level(ctx: _Ctx, links, device_id: int, depth: int) -> dict:
    if device_id not in ctx.devices:
        raise HTTPException(404, f"Device {device_id} not found")
    adjacency: dict[int, set[int]] = defaultdict(set)
    for link in links:
        if link.a_device_id in ctx.devices and link.b_device_id in ctx.devices and link.a_device_id != link.b_device_id:
            adjacency[link.a_device_id].add(link.b_device_id)
            adjacency[link.b_device_id].add(link.a_device_id)
    included, frontier, truncated = {device_id}, {device_id}, False
    for _ in range(depth):
        nxt = set()
        for node in frontier:
            for other in sorted(adjacency[node]):
                if other not in included:
                    if len(included) >= MAX_NEIGHBOR_NODES:
                        truncated = True
                        break
                    included.add(other)
                    nxt.add(other)
        frontier = nxt
    edges: dict[tuple[str, str, str], _Agg] = defaultdict(_Agg)
    totals: dict[str, _Agg] = defaultdict(_Agg)
    for link in links:
        if link.a_device_id in included and link.b_device_id in included and link.a_device_id != link.b_device_id:
            segment = _segment_of(ctx, link)
            edges[(*_pair(f"dev:{link.a_device_id}", f"dev:{link.b_device_id}"), segment)].add(link)
            totals[segment].add(link)
    center = ctx.devices[device_id]
    nodes = [_device_node(ctx, ctx.devices[i], i == device_id)
             for i in sorted(included, key=lambda i: (ROLE_TIER.get(ctx.devices[i].role, 4), ctx.devices[i].hostname))]
    _place_devices(ctx, nodes)
    return {"level": "neighbors", "device": center.hostname, "device_id": device_id, "depth": depth,
            "pop": ctx.pop_code(device_id), **_path_fields(ctx.path_of_pop(center.pop_id)), "truncated": truncated,
            "nodes": nodes, "edges": _edges(edges), "segments": _segments(totals)}


def _median(values: list[float]) -> float:
    ordered = sorted(values)
    mid = len(ordered) // 2
    return ordered[mid] if len(ordered) % 2 else (ordered[mid - 1] + ordered[mid]) / 2


def _area_centers(ctx: _Ctx) -> dict[str, tuple[float, float]]:
    """(lat, lon) per area: median of its POPs that have coordinates, else the area's default spot."""
    points: dict[str, list[tuple[float, float]]] = defaultdict(list)
    for pid, pop in ctx.pops.items():
        if pop.latitude is not None and pop.longitude is not None:
            points[ctx.area_of_pop[pid]].append((pop.latitude, pop.longitude))
    centers = {}
    for area in set(ctx.area_of_pop.values()) | set(AREAS):
        if points.get(area):
            centers[area] = (_median([p[0] for p in points[area]]), _median([p[1] for p in points[area]]))
        elif (pos := area_position(area)) is not None:
            centers[area] = (pos[1], pos[0])
    return centers


def _estimate_positions(known: dict[str, tuple[float, float]], neighbours: dict[str, set[str]],
                        missing: list[str], fallback: tuple[float, float] | None) -> dict[str, tuple[float, float]]:
    """Display-only spot for nodes without coordinates: the mean of their already placed neighbours
    (spread out a little so several do not stack), repeated so chains get placed too; the rest
    around `fallback` on a ring. Never stored."""
    placed = dict(known)
    estimated: dict[str, tuple[float, float]] = {}
    for _ in range(6):
        progress = False
        for node in missing:
            if node in placed:
                continue
            around = [placed[n] for n in neighbours.get(node, ()) if n in placed]
            if around:
                offset = (sum(map(ord, node)) % 12) / 12 * 6.283
                lat = sum(p[0] for p in around) / len(around) + 0.08 * math.sin(offset)
                lon = sum(p[1] for p in around) / len(around) + 0.08 * math.cos(offset)
                placed[node] = estimated[node] = (lat, lon)
                progress = True
        if not progress:
            break
    rest = [n for n in missing if n not in placed]
    if fallback:
        for i, node in enumerate(rest):
            angle = i / max(len(rest), 1) * 6.283
            radius = 0.35 + 0.1 * (i // 24)
            estimated[node] = (fallback[0] + radius * math.sin(angle), fallback[1] + radius * math.cos(angle))
    return estimated


def _area_level(ctx: _Ctx, links) -> dict:
    inner: dict[str, _Agg] = defaultdict(_Agg)
    trunks: dict[tuple[str, str, str], _Agg] = defaultdict(_Agg)
    totals: dict[str, _Agg] = defaultdict(_Agg)
    for link in links:
        if link.a_device_id not in ctx.devices or link.b_device_id not in ctx.devices:
            continue
        segment = _segment_of(ctx, link)
        totals[segment].add(link)
        a, b = ctx.area_of_device(link.a_device_id), ctx.area_of_device(link.b_device_id)
        if a == b:
            inner[a].add(link)
        else:
            trunks[(*_pair(f"area:{a}", f"area:{b}"), segment)].add(link)
    pops_per_area: Counter = Counter(ctx.area_of_pop.values())
    devices_per_area: Counter = Counter(ctx.area_of_device(d) for d in ctx.devices)
    names = [a for a in AREA_ORDER if pops_per_area[a]] + sorted(set(pops_per_area) - set(AREA_ORDER))
    centers = _area_centers(ctx)
    located = Counter(ctx.area_of_pop[pid] for pid, p in ctx.pops.items() if p.latitude is not None)
    nodes = []
    for name in names:
        lat, lon = centers.get(name, (None, None))
        nodes.append({
            "id": f"area:{name}", "kind": "area", "label": name, "pops": pops_per_area[name],
            "devices": devices_per_area[name], "internal": inner[name].as_dict(),
            "lon": lon, "lat": lat, "pops_located": located[name],
        })
    return {"level": "area", "nodes": nodes, "edges": _edges(trunks), "segments": _segments(totals)}


# Drill-down groups by depth in the (area, province, regency) path of a POP.
_GROUP_KINDS = ("area", "province", "regency")
_GROUP_LEVELS = ("province", "regency", "pop")  # what opening a group of each depth shows


def _group_node_id(path: tuple[str, ...]) -> str:
    """area:Jawa Timur · province:Jawa Timur|Jawa Timur · regency:Jawa Timur|Jawa Timur|Kota Malang."""
    return f"{_GROUP_KINDS[len(path) - 1]}:{'|'.join(path)}"


def _group_centers(ctx: _Ctx) -> dict[tuple[str, ...], tuple[float, float]]:
    """(lat, lon) per area / province / regency: median of its POPs' spots (coordinates or city centre),
    areas without any falling back to their default spot."""
    points: dict[tuple[str, ...], list[tuple[float, float]]] = defaultdict(list)
    for pid in ctx.pops:
        spot = ctx.pop_spot(pid)
        if spot is None:
            continue
        path = ctx.path_of_pop(pid)
        for depth in (1, 2, 3):
            points[path[:depth]].append(spot)
    centers = {key: (_median([p[0] for p in pts]), _median([p[1] for p in pts])) for key, pts in points.items()}
    for area, spot in _area_centers(ctx).items():
        centers.setdefault((area,), spot)
    return centers


def _outside_path(path: tuple[str, ...], scope: tuple[str, ...]) -> tuple[str, ...]:
    """The group a POP outside `scope` is drawn as: the widest one where it leaves the scope
    (another area, else another province of this area, else another regency of this province)."""
    for depth, (mine, theirs) in enumerate(zip(scope, path), start=1):
        if mine != theirs:
            return path[:depth]
    return path[:len(scope)]


def _scoped_level(ctx: _Ctx, links, scope: tuple[str, ...], pops: bool) -> dict:
    """POPs inside `scope` (a path prefix), drawn as POPs (`pops`) or as the groups one level below the
    scope, with links out of the scope ending on the group they lead to. A group level holding a single
    group opens that group instead."""
    inside = {pid for pid in ctx.pops if ctx.path_of_pop(pid)[:len(scope)] == scope}
    if not inside:
        raise HTTPException(404, f"No POPs in {' / '.join(scope)}")
    if not pops:
        groups = {ctx.path_of_pop(pid)[:len(scope) + 1] for pid in inside}
        if len(groups) == 1:
            only = groups.pop()
            return _scoped_level(ctx, links, only, pops=len(only) == 3)

    def node_of(pid: int) -> str:
        path = ctx.path_of_pop(pid)
        if pid not in inside:
            return _group_node_id(_outside_path(path, scope))
        return f"pop:{ctx.pops[pid].code}" if pops else _group_node_id(path[:len(scope) + 1])

    inner: dict[str, _Agg] = defaultdict(_Agg)
    edges: dict[tuple[str, str, str], _Agg] = defaultdict(_Agg)
    totals: dict[str, _Agg] = defaultdict(_Agg)
    external: Counter = Counter()
    for link in links:
        if link.a_device_id not in ctx.devices or link.b_device_id not in ctx.devices:
            continue
        pa, pb = ctx.devices[link.a_device_id].pop_id, ctx.devices[link.b_device_id].pop_id
        if pa not in inside and pb not in inside:
            continue
        segment = _segment_of(ctx, link)
        totals[segment].add(link)
        na, nb = node_of(pa), node_of(pb)
        if na == nb:
            inner[na].add(link)
            continue
        edges[(*_pair(na, nb), segment)].add(link)
        for pid, nid in ((pa, na), (pb, nb)):
            if pid not in inside:
                external[nid] += 1

    centers = _group_centers(ctx)
    nodes = _pop_nodes(ctx, inside, inner) if pops else _group_nodes(ctx, inside, scope, inner, centers)
    outside_paths = {_group_node_id(_outside_path(ctx.path_of_pop(pid), scope)): _outside_path(ctx.path_of_pop(pid), scope)
                     for pid in ctx.pops if pid not in inside}
    for nid, count in external.items():
        path = outside_paths[nid]
        lat, lon = centers.get(path, (None, None))
        nodes.append({"id": nid, "kind": f"external_{_GROUP_KINDS[len(path) - 1]}", "label": path[-1],
                      "links": count, "lat": lat, "lon": lon, **_path_fields(path)})
    if pops:
        _place_pops(nodes, edges, centers.get(scope))
    level = "pop" if pops else _GROUP_LEVELS[len(scope) - 1]
    return {"level": level, **_path_fields(scope), "nodes": nodes, "edges": _edges(edges), "segments": _segments(totals)}


def _group_nodes(ctx: _Ctx, inside: set[int], scope: tuple[str, ...], inner: dict[str, _Agg],
                 centers: dict[tuple[str, ...], tuple[float, float]]) -> list[dict]:
    pops_of: dict[tuple[str, ...], list[int]] = defaultdict(list)
    for pid in inside:
        pops_of[ctx.path_of_pop(pid)[:len(scope) + 1]].append(pid)
    devices_per_pop = Counter(d.pop_id for d in ctx.devices.values())
    nodes = []
    # Unmapped groups last, the rest by name.
    for path in sorted(pops_of, key=lambda p: ("belum dipetakan" in p[-1].lower(), p[-1])):
        lat, lon = centers.get(path, (None, None))
        members = pops_of[path]
        nid = _group_node_id(path)
        nodes.append({
            "id": nid, "kind": _GROUP_KINDS[len(path) - 1], "label": path[-1], **_path_fields(path),
            "pops": len(members), "devices": sum(devices_per_pop[p] for p in members),
            "internal": inner[nid].as_dict(), "lat": lat, "lon": lon,
            "pops_located": sum(ctx.pops[p].latitude is not None for p in members),
        })
    return nodes


def _pop_nodes(ctx: _Ctx, inside: set[int], inner: dict[str, _Agg]) -> list[dict]:
    devices_per_pop: dict[int, list[Any]] = defaultdict(list)
    for d in ctx.devices.values():
        if d.pop_id in inside:
            devices_per_pop[d.pop_id].append(d)
    labels = _pop_labels(ctx, inside)
    nodes = []
    for pid in sorted(inside, key=lambda p: labels[p]):
        p = ctx.pops[pid]
        devs = devices_per_pop[pid]
        nodes.append({
            "id": f"pop:{p.code}", "kind": "pop", "label": labels[pid], "code": p.code, "name": p.name,
            **_path_fields(ctx.path_of_pop(pid)), "pop_id": pid, "region": p.region, "city": p.city,
            "devices": len(devs), "sites": len({site_for(d.hostname, p.code) for d in devs}),
            "categories": dict(Counter(category_for(d.role) for d in devs)),
            # Highest role in the POP: core POPs (P/DCI) sit in the middle of the ring layout.
            "tier": min((ROLE_TIER.get(d.role, 4) for d in devs), default=4),
            "internal": inner[f"pop:{p.code}"].as_dict(), "lat": p.latitude, "lon": p.longitude,
        })
    return nodes


def _place_pops(nodes: list[dict], edges: dict, center: tuple[float, float] | None) -> None:
    """Fill lat/lon of POPs without coordinates with a display estimate, flagged `estimated`: the
    centre of the city its code names, else near its linked POPs, else around the area centre."""
    for node in nodes:
        if node["kind"] == "pop" and node.get("lat") is None and (spot := city_center_for_code(node["code"])):
            node["lat"], node["lon"], node["estimated"] = spot[0], spot[1], True
    known = {n["id"]: (n["lat"], n["lon"]) for n in nodes if n.get("lat") is not None and n.get("lon") is not None}
    neighbours: dict[str, set[str]] = defaultdict(set)
    for a, b, _segment in edges:
        neighbours[a].add(b)
        neighbours[b].add(a)
    missing = [n["id"] for n in nodes if n["kind"] == "pop" and n["id"] not in known]
    estimated = _estimate_positions(known, neighbours, missing, center)
    for node in nodes:
        node["estimated"] = node.get("estimated", False) or node["id"] in estimated
        if node["id"] in estimated:
            node["lat"], node["lon"] = estimated[node["id"]]


def _device_level(ctx: _Ctx, links, pop_code: str) -> dict:
    pop = next((p for p in ctx.pops.values() if p.code.upper() == pop_code.upper()), None)
    if pop is None:
        raise HTTPException(404, f"POP '{pop_code}' not found")
    local = {d.id: d for d in ctx.devices.values() if d.pop_id == pop.id}
    edges: dict[tuple[str, str, str], _Agg] = defaultdict(_Agg)
    totals: dict[str, _Agg] = defaultdict(_Agg)
    external: dict[str, set[int]] = defaultdict(set)
    for link in links:
        a_in, b_in = link.a_device_id in local, link.b_device_id in local
        if not (a_in or b_in) or link.a_device_id not in ctx.devices or link.b_device_id not in ctx.devices:
            continue
        segment = _segment_of(ctx, link)
        totals[segment].add(link)
        if a_in and b_in:
            edges[(*_pair(f"dev:{link.a_device_id}", f"dev:{link.b_device_id}"), segment)].add(link)
        else:
            mine, theirs = (link.a_device_id, link.b_device_id) if a_in else (link.b_device_id, link.a_device_id)
            remote_pop = ctx.pop_code(theirs)
            edges[(*_pair(f"dev:{mine}", f"pop:{remote_pop}"), segment)].add(link)
            external[remote_pop].add(theirs)

    nodes = [_device_node(ctx, d)
             for d in sorted(local.values(), key=lambda d: (ROLE_TIER.get(d.role, 4), d.hostname))]
    by_code = {p.code: p for p in ctx.pops.values()}
    remote_ids = {by_code[code].id for code in external if code in by_code}
    labels = _pop_labels(ctx, remote_ids)
    for code, remote_devices in sorted(external.items()):
        rp = by_code.get(code)
        nodes.append({
            "id": f"pop:{code}", "kind": "external_pop", "label": labels.get(rp.id, code) if rp else code, "code": code,
            **(_path_fields(ctx.path_of_pop(rp.id)) if rp else {"area": UNMAPPED_AREA}),
            "devices": len(remote_devices),
        })
    _place_devices(ctx, nodes)
    return {"level": "device", "pop": pop.code, "pop_name": pop.name, "pop_id": pop.id, "region": pop.region,
            "city": ctx.place_of_pop[pop.id].city, **_path_fields(ctx.path_of_pop(pop.id)), "nodes": nodes,
            "edges": _edges(edges), "segments": _segments(totals)}


# --------------------------------------------------------------------------- search & POP area


@router.get("/topology/search")
def topology_search(q: str = Query(..., min_length=2), limit: int = Query(15, ge=1, le=50),
                    db: Session = Depends(get_db)):
    """Hostname or POP code/name, for the topology search box: exact matches first, then prefix, then
    substring. Each hit says where to open it (device -> its neighbours, POP -> its devices)."""
    needle = q.strip().upper()
    ctx = _context(db)

    def rank(text: str) -> int:
        return 0 if text == needle else 1 if text.startswith(needle) else 2

    pops = [p for p in ctx.pops.values() if needle in p.code.upper() or needle in (p.name or "").upper()]
    pops.sort(key=lambda p: (rank(p.code.upper()), p.code))
    devices = [d for d in ctx.devices.values() if needle in d.hostname.upper()]
    devices.sort(key=lambda d: (rank(d.hostname.upper()), len(d.hostname), d.hostname))
    return {
        "pops": [{"pop_id": p.id, "code": p.code, "name": p.name, "area": ctx.area_of_pop[p.id],
                  "devices": sum(1 for d in ctx.devices.values() if d.pop_id == p.id)} for p in pops[:limit]],
        "devices": [{"device_id": d.id, "hostname": d.hostname, "pop": ctx.pop_code(d.id), "role": d.role,
                     "category": category_for(d.role), "area": ctx.area_of_device(d.id)} for d in devices[:limit]],
    }


@router.get("/areas/options")
def area_options(db: Session = Depends(get_db)):
    """Area names a POP can be assigned to: the standard areas plus any custom region already in use."""
    used = {(p.region or "").strip() for p in db.scalars(select(m.Pop))} - {"", UNMAPPED_AREA}
    return {"areas": list(AREAS) + sorted(used - set(AREAS))}


class PopAreaIn(BaseModel):
    area: str | None = Field(default=None, max_length=64, description="Empty clears it (back to automatic)")


@router.put("/pops/{pop_id}/area")
def set_pop_area(pop_id: int, payload: PopAreaIn, db: Session = Depends(get_db)):
    """Assign a POP to an area by hand. A curated area always wins over the automatic mapping."""
    pop = db.get(m.Pop, pop_id)
    if pop is None:
        raise HTTPException(404, f"POP {pop_id} not found")
    area = (payload.area or "").strip()
    pop.region = None if not area or area == UNMAPPED_AREA else area
    db.commit()
    return {"pop_id": pop.id, "code": pop.code, "region": pop.region,
            "area": area_for_pop(pop.code, pop.region, pop.name)}


# --------------------------------------------------------------------------- device summary


@router.get("/inventory/devices/summary")
def device_summary(db: Session = Depends(get_db)):
    """Device counts per category, with the role, vendor, status and area split of each category."""
    return cache.cached(("inventory", "devices-summary"), lambda: _device_summary(db))


def _device_summary(db: Session) -> dict:
    ctx = _context(db)
    per: dict[str, dict[str, Counter]] = defaultdict(lambda: defaultdict(Counter))
    totals: Counter = Counter()
    for d in ctx.devices.values():
        cat = category_for(d.role)
        totals[cat] += 1
        per[cat]["roles"][d.role] += 1
        per[cat]["vendors"][ctx.vendors.get(d.vendor_id or -1) or "Unknown"] += 1
        per[cat]["status"][d.status] += 1
        per[cat]["areas"][ctx.area_of_device(d.id)] += 1
    categories = [
        {"category": c, "label": CATEGORY_LABELS[c], "devices": totals[c],
         **{k: dict(v.most_common()) for k, v in per[c].items()}}
        for c in CATEGORY_ORDER if totals[c]
    ]
    return {"total": sum(totals.values()), "categories": categories}


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
            "used_pct": occupancy_pct(self.used, self.total),
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
    """Physical ports Observium reports, grouped per device/speed/state in SQL. Only those count: their speed is
    Observium's ifHighSpeed/ifSpeed. Logical interfaces are not physical, and ports known only from a config file
    carry a speed guessed from the name."""
    return db.execute(
        select(m.Interface.device_id, m.Interface.speed_gbps, m.Interface.admin_status, m.Interface.oper_status,
               func.count())
        .join(m.ObserviumPort, m.ObserviumPort.interface_id == m.Interface.id)
        .where(m.Interface.if_type == "physical")
        .group_by(m.Interface.device_id, m.Interface.speed_gbps, m.Interface.admin_status, m.Interface.oper_status)
    ).all()


def _config_only_ports(db: Session) -> dict[int, int]:
    """Physical ports per device that Observium does not report (left out of port capacity)."""
    return dict(db.execute(
        select(m.Interface.device_id, func.count())
        .outerjoin(m.ObserviumPort, m.ObserviumPort.interface_id == m.Interface.id)
        .where(m.Interface.if_type == "physical", m.ObserviumPort.interface_id.is_(None))
        .group_by(m.Interface.device_id)
    ).all())


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
    category: str | None = Query(None, pattern=CATEGORY_PATTERN),
    area: str | None = None,
    pop: str | None = None,
    vendor: str | None = None,
    db: Session = Depends(get_db),
):
    """Physical ports by speed: used (oper up) / idle (admin up, oper down) / disabled (admin down)."""
    key = ("ports", "summary", category, area, (pop or "").upper(), (vendor or "").lower())
    return cache.cached(key, lambda: _port_summary(db, category, area, pop, vendor))


def _port_summary(db: Session, category, area, pop, vendor) -> dict:
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
    order = CATEGORY_ORDER
    config_only = sum(n for device_id, n in _config_only_ports(db).items()
                      if _device_matches(ctx, device_id, category, area, pop, vendor))
    return {
        "filters": {"category": category, "area": area, "pop": pop, "vendor": vendor},
        "totals": overall.summary(),
        "config_only_ports": config_only,
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
    category: str | None = Query(None, pattern=CATEGORY_PATTERN),
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
    rows = device_port_usage(db, category, area, pop, vendor, speed)
    needle = (q or "").lower()
    if needle:
        rows = [r for r in rows if needle in r["hostname"].lower()]
    reverse = sort != "hostname"
    rows = sorted(rows, key=lambda r: r[sort] if sort != "hostname" else r["hostname"], reverse=reverse)
    return {"total_devices": len(rows), "devices": rows[:limit]}


def device_port_usage(db: Session, category: str | None = None, area: str | None = None, pop: str | None = None,
                      vendor: str | None = None, speed: str | None = None) -> list[dict[str, Any]]:
    """Port usage per device (physical ports; used = oper up). Cached: shared by the ports page and hotspots."""
    key = ("ports", "devices", category, area, (pop or "").upper(), (vendor or "").lower(), speed)
    return cache.cached(key, lambda: _device_port_usage(db, category, area, pop, vendor, speed))


def _device_port_usage(db: Session, category, area, pop, vendor, speed) -> list[dict[str, Any]]:
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
    for device_id, u in usage.items():
        d = ctx.devices[device_id]
        rows.append({
            "device_id": device_id, "hostname": d.hostname, "pop": ctx.pop_code(device_id),
            "area": ctx.area_of_device(device_id), "site": site_for(d.hostname, ctx.pop_code(device_id)),
            "role": d.role, "category": category_for(d.role), "vendor": ctx.vendors.get(d.vendor_id or -1),
            **u.summary(),
            "speeds": {r["speed"]: {"used": r["used"], "idle": r["idle"], "disabled": r["disabled"]} for r in u.speeds()},
        })
    return rows
