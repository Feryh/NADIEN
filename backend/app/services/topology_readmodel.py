"""Read-model helpers shared by the topology views (area/province/regency/POP/device levels and the city view).

Kept out of the router so services can build topology data without importing a route module.
"""

import math
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models as m
from .areas import (
    ROLE_TIER,
    UNMAPPED_AREA,
    area_for_hostnames,
    area_for_pop,
    category_for,
    city_center_for_code,
    site_for,
)
from .capacity import capacity_color
from .regions import Place, place_for_pop
from .segments import SEGMENTS, link_segment

UNASSIGNED_POP = "UNASSIGNED"


# --------------------------------------------------------------------------- shared context


@dataclass
class Ctx:
    pops: dict[int, m.Pop]
    area_of_pop: dict[int, str]
    devices: dict[int, Any]  # rows: id, hostname, pop_id, role, vendor_id, status
    vendors: dict[int, str]
    locations: dict[int, tuple[float, float]] = field(default_factory=dict)  # device's own site (lat, lon)
    place_of_pop: dict[int, Place] = field(default_factory=dict)

    def path_of_pop(self, pop_id: int) -> tuple[str, str, str]:
        """(area, province, regency): where the POP sits in the drill-down."""
        place = self.place_of_pop.get(pop_id) or place_for_pop("UNASSIGNED", None, None)
        return self.area_of_pop.get(pop_id, UNMAPPED_AREA), place.province, place.regency

    def pop_spot(self, pop_id: int) -> tuple[float, float] | None:
        """POP coordinates, else the centre of the city its code names (estimate)."""
        pop = self.pops.get(pop_id)
        if pop is None:
            return None
        if pop.latitude is not None and pop.longitude is not None:
            return pop.latitude, pop.longitude
        return city_center_for_code(pop.code)

    def area_of_device(self, device_id: int) -> str:
        return self.area_of_pop.get(self.devices[device_id].pop_id, UNMAPPED_AREA)

    def pop_code(self, device_id: int) -> str:
        pop = self.pops.get(self.devices[device_id].pop_id)
        return pop.code if pop else "?"


def context(db: Session) -> Ctx:
    pops = {p.id: p for p in db.scalars(select(m.Pop))}
    devices = {
        r.id: r
        for r in db.execute(select(m.Device.id, m.Device.hostname, m.Device.pop_id, m.Device.role,
                                   m.Device.vendor_id, m.Device.status, m.Device.map_x, m.Device.map_y))
    }
    area_of_pop = {pid: area_for_pop(p.code, p.region, p.name) for pid, p in pops.items()}
    hostnames_by_pop: dict[int, list[str]] = defaultdict(list)
    for device in devices.values():
        hostnames_by_pop[device.pop_id].append(device.hostname)
    for pid, area in area_of_pop.items():
        if area == UNMAPPED_AREA and pops[pid].code.upper() != UNASSIGNED_POP:
            area_of_pop[pid] = area_for_hostnames(hostnames_by_pop[pid]) or UNMAPPED_AREA

    return Ctx(
        pops=pops,
        area_of_pop=area_of_pop,
        devices=devices,
        vendors={v.id: v.name for v in db.scalars(select(m.Vendor))},
        locations={r.device_id: (r.latitude, r.longitude) for r in
                   db.execute(select(m.DeviceLocation.device_id, m.DeviceLocation.latitude, m.DeviceLocation.longitude))},
        place_of_pop={pid: place_for_pop(p.code, p.city, p.name, area_of_pop[pid], hostnames_by_pop[pid])
                      for pid, p in pops.items()},
    )


DEVICE_RING_DEGREES = 0.004  # ~400 m: devices without their own site circle their POP on the map


def place_devices(ctx: Ctx, nodes: list[dict]) -> None:
    """lat/lon for device and external-POP nodes: a device's own site, else a small ring around its POP
    (estimated, drawn dashed); other POPs at their coordinates or city centre."""
    ring: dict[int, int] = defaultdict(int)
    for node in nodes:
        if node["kind"] == "device":
            own = ctx.locations.get(node["device_id"])
            if own:
                node["lat"], node["lon"], node["estimated"], node["located"] = own[0], own[1], False, True
                continue
            pop_id = ctx.devices[node["device_id"]].pop_id
            spot = ctx.pop_spot(pop_id)
            node["located"] = False
            node["estimated"] = True
            if spot:
                i = ring[pop_id]
                ring[pop_id] += 1
                angle = i * 2.39996  # golden angle: an even spiral however many devices
                radius = DEVICE_RING_DEGREES * math.sqrt(i + 1)
                node["lat"], node["lon"] = spot[0] + radius * math.sin(angle), spot[1] + radius * math.cos(angle)
        elif node["kind"] == "external_pop":
            pop = next((p for p in ctx.pops.values() if p.code == node["code"]), None)
            spot = ctx.pop_spot(pop.id) if pop else None
            if spot:
                node["lat"], node["lon"] = spot


@dataclass
class Agg:
    links: int = 0
    down: int = 0
    capacity: float = 0.0
    current: float = 0.0
    reserved: float = 0.0
    unknown: int = 0  # links whose capacity is not known (0): never counted as "green"

    def add(self, link: Any) -> None:
        self.links += 1
        self.down += link.status == "down"
        self.unknown += not link.capacity_gbps
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
            "capacity_unknown": self.unknown,
        }


def load_links(db: Session):
    return db.execute(select(m.Link.id, m.Link.a_device_id, m.Link.b_device_id, m.Link.capacity_gbps,
                             m.Link.current_gbps, m.Link.reserved_gbps, m.Link.status)).all()


def edges_of(aggs: dict[tuple[str, str, str], Agg]) -> list[dict[str, Any]]:
    """One edge per node pair and link function, so each segment can be shown, filtered and monitored alone."""
    return [{"id": f"{a}|{b}|{segment}", "source": a, "target": b, "segment": segment, **agg.as_dict()}
            for (a, b, segment), agg in aggs.items()]


def segment_of(ctx: Ctx, link: Any) -> str:
    return link_segment(ctx.devices[link.a_device_id].role, ctx.devices[link.b_device_id].role)


def segments_of(totals: dict[str, Agg]) -> dict[str, dict[str, Any]]:
    """Monitoring summary per link function for everything in the view's scope (internal links included)."""
    return {segment: totals[segment].as_dict() for segment in SEGMENTS}


def pair(a: str, b: str) -> tuple[str, str]:
    return (a, b) if a <= b else (b, a)


def device_node(ctx: Ctx, d: Any, center: bool = False) -> dict:
    code = ctx.pop_code(d.id)
    return {
        "id": f"dev:{d.id}", "kind": "device", "device_id": d.id, "label": d.hostname, "pop": code,
        "site": f"site:{site_for(d.hostname, code)}", "role": d.role, "category": category_for(d.role),
        "tier": ROLE_TIER.get(d.role, 4), "vendor": ctx.vendors.get(d.vendor_id or -1), "status": d.status,
        "x": d.map_x, "y": d.map_y, "center": center,
    }


def path_fields(path: tuple[str, ...]) -> dict[str, str]:
    return dict(zip(("area", "province", "regency"), path))


def pop_labels(ctx: Ctx, pop_ids: set[int]) -> dict[int, str]:
    """City name per POP; POPs sharing a city in one view get their code added (Jakarta · JKT2)."""
    city = {pid: ctx.place_of_pop[pid].city or ctx.pops[pid].code for pid in pop_ids}
    shared = Counter(city.values())
    return {pid: f"{name} · {ctx.pops[pid].code}" if shared[name] > 1 and name != ctx.pops[pid].code else name
            for pid, name in city.items()}
