"""Customer path engineering (Phase 4).

Devices are nodes and up links are edges. A path is valid when every link has room for the
requested bandwidth without passing the utilization ceiling (planning threshold by default):

    (current + reserved + bandwidth) / capacity <= max_util

Primary = cheapest path by latency (or hops). Backup, for protected services, is searched with
decreasing diversity until one exists:
    1. fully node-disjoint, including different endpoint devices (dual-homed POPs);
    2. node-disjoint transit (same endpoint devices);
    3. link-disjoint.
Links on the same fiber as the primary (shared-risk) are always excluded from the backup.

Latency per link: measured `latency_ms` when present, else estimated — 0.05 ms inside a POP,
distance × 5 µs/km, the great-circle distance between POP (or area-centre) coordinates × 1.4
route factor, 1 ms between POPs of one area, else a pessimistic 5 ms. Estimated values are
flagged so they're never mistaken for measurements.
"""

import heapq
import math
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models as m
from ..config import settings

US_PER_KM = 0.005  # ms per km of fibre (~5 µs/km)
ROUTE_FACTOR = 1.4  # fibre route vs straight line
INTRA_POP_MS = 0.05
SAME_AREA_MS = 1.0  # two POPs in one area, exact distance unknown
# No location at all: deliberately pessimistic, so a route through unmapped POPs never looks
# faster than one whose length is known or estimated.
UNKNOWN_HOP_MS = 5.0


@dataclass
class Edge:
    link_id: int
    a: int
    b: int
    capacity: float
    current: float
    reserved: float
    status: str
    latency_ms: float | None
    distance_km: float | None
    fiber_id: int | None
    name: str = ""

    def other(self, node: int) -> int:
        return self.b if node == self.a else self.a

    @property
    def available(self) -> float:
        return max(self.capacity - self.current - self.reserved, 0.0)


@dataclass
class Graph:
    adjacency: dict[int, list[Edge]]
    edges: dict[int, Edge]
    hostname: dict[int, str]
    pop_of: dict[int, int]
    pop_code: dict[int, str]
    pop_coords: dict[int, tuple[float, float]]

    def latency(self, edge: Edge) -> tuple[float, bool]:
        if edge.latency_ms is not None:
            return edge.latency_ms, False
        pa, pb = self.pop_of.get(edge.a), self.pop_of.get(edge.b)
        if pa is not None and pa == pb:
            return INTRA_POP_MS, True
        if edge.distance_km:
            return edge.distance_km * US_PER_KM, True
        if pa in self.pop_coords and pb in self.pop_coords:
            km = _haversine_km(self.pop_coords[pa], self.pop_coords[pb])
            if km > 1:
                return km * ROUTE_FACTOR * US_PER_KM, True
            return SAME_AREA_MS, True  # both fell back to the same area centre
        return UNKNOWN_HOP_MS, True


def _haversine_km(a: tuple[float, float], b: tuple[float, float]) -> float:
    lat1, lon1, lat2, lon2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 6371 * 2 * math.asin(math.sqrt(h))


def build_graph(db: Session) -> Graph:
    pops = {p.id: p for p in db.scalars(select(m.Pop))}
    devices = db.execute(select(m.Device.id, m.Device.hostname, m.Device.pop_id)).all()
    edges: dict[int, Edge] = {}
    adjacency: dict[int, list[Edge]] = {d.id: [] for d in devices}
    for l in db.scalars(select(m.Link)):
        if l.a_device_id == l.b_device_id or l.a_device_id not in adjacency or l.b_device_id not in adjacency:
            continue
        e = Edge(l.id, l.a_device_id, l.b_device_id, l.capacity_gbps or 0, l.current_gbps or 0, l.reserved_gbps or 0,
                 l.status, l.latency_ms, l.distance_km, l.fiber_id, l.name)
        edges[l.id] = e
        adjacency[e.a].append(e)
        adjacency[e.b].append(e)
    return Graph(
        adjacency=adjacency,
        edges=edges,
        hostname={d.id: d.hostname for d in devices},
        pop_of={d.id: d.pop_id for d in devices},
        pop_code={pid: p.code for pid, p in pops.items()},
        pop_coords=_pop_coords(pops),
    )


def _pop_coords(pops: dict[int, m.Pop]) -> dict[int, tuple[float, float]]:
    """POP coordinates; POPs without any fall back to their area's centre, so a JKT–SBY hop is
    estimated from ~660 km rather than a flat default. Both are estimates and flagged as such."""
    from .areas import area_for_pop, area_position

    coords = {}
    for pid, p in pops.items():
        if p.latitude is not None and p.longitude is not None:
            coords[pid] = (p.latitude, p.longitude)
            continue
        centre = area_position(area_for_pop(p.code, p.region, p.name))
        if centre is not None:
            coords[pid] = (centre[1], centre[0])  # area_position is (lon, lat)
    return coords


@dataclass
class Constraints:
    bandwidth_gbps: float
    max_util_pct: float
    metric: str = "latency"  # latency | hops
    banned_nodes: set[int] = field(default_factory=set)
    banned_links: set[int] = field(default_factory=set)
    banned_fibers: set[int] = field(default_factory=set)
    check_capacity: bool = True

    def allows(self, edge: Edge) -> bool:
        if edge.status != "up" or edge.link_id in self.banned_links:
            return False
        if edge.fiber_id is not None and edge.fiber_id in self.banned_fibers:
            return False
        if not self.check_capacity:
            return True
        if edge.capacity <= 0 or edge.available < self.bandwidth_gbps:
            return False
        return (edge.current + edge.reserved + self.bandwidth_gbps) / edge.capacity * 100 <= self.max_util_pct + 1e-9


@dataclass
class Path:
    devices: list[int]
    edges: list[Edge]

    @property
    def hops(self) -> int:
        return len(self.edges)


def shortest_path(graph: Graph, sources: set[int], targets: set[int], c: Constraints) -> Path | None:
    """Multi-source Dijkstra; ties broken by fewer hops."""
    dist: dict[int, tuple[float, int]] = {}
    prev: dict[int, tuple[int, Edge]] = {}
    heap: list[tuple[float, int, int]] = []
    for s in sources:
        if s in graph.adjacency and s not in c.banned_nodes:
            dist[s] = (0.0, 0)
            heapq.heappush(heap, (0.0, 0, s))
    while heap:
        cost, hops, node = heapq.heappop(heap)
        if (cost, hops) > dist.get(node, (math.inf, 0)):
            continue
        if node in targets:
            devices, edges, cur = [node], [], node
            while cur in prev:
                cur, edge = prev[cur]
                devices.append(cur)
                edges.append(edge)
            return Path(devices[::-1], edges[::-1])
        for edge in graph.adjacency.get(node, ()):
            nxt = edge.other(node)
            if nxt in c.banned_nodes and nxt not in targets:
                continue
            if not c.allows(edge):
                continue
            step = 1.0 if c.metric == "hops" else graph.latency(edge)[0]
            cand = (cost + step, hops + 1)
            if cand < dist.get(nxt, (math.inf, 0)):
                dist[nxt] = cand
                prev[nxt] = (node, edge)
                heapq.heappush(heap, (cand[0], cand[1], nxt))
    return None


def describe(graph: Graph, path: Path, bandwidth_gbps: float) -> dict[str, Any]:
    hops, total, estimated = [], 0.0, False
    for i, e in enumerate(path.edges):
        frm, to = path.devices[i], path.devices[i + 1]
        lat, est = graph.latency(e)
        total += lat
        estimated |= est
        after = (e.current + e.reserved + bandwidth_gbps) / e.capacity * 100 if e.capacity else None
        hops.append({
            "link_id": e.link_id,
            "from": graph.hostname[frm], "to": graph.hostname[to],
            "from_pop": graph.pop_code.get(graph.pop_of.get(frm)), "to_pop": graph.pop_code.get(graph.pop_of.get(to)),
            "capacity_gbps": round(e.capacity, 3), "current_gbps": round(e.current, 3),
            "reserved_gbps": round(e.reserved, 3), "available_gbps": round(e.available, 3),
            "after_pct": round(after, 1) if after is not None else None,
            "latency_ms": round(lat, 3), "latency_estimated": est, "fiber_id": e.fiber_id,
        })
    bottleneck = min(path.edges, key=lambda e: e.available) if path.edges else None
    return {
        "device_ids": path.devices,
        "devices": [graph.hostname[d] for d in path.devices],
        "pops": list(dict.fromkeys(graph.pop_code.get(graph.pop_of.get(d)) for d in path.devices)),
        "link_ids": [e.link_id for e in path.edges],
        "hops": path.hops,
        "latency_ms": round(total, 3),
        "latency_estimated": estimated,
        "bottleneck_available_gbps": round(bottleneck.available, 3) if bottleneck else None,
        "max_after_pct": max((h["after_pct"] or 0 for h in hops), default=0),
        "hop_detail": hops,
    }


def compute(
    graph: Graph,
    a_devices: set[int],
    b_devices: set[int],
    bandwidth_gbps: float,
    protection: str = "none",
    max_util_pct: float | None = None,
    metric: str = "latency",
) -> dict[str, Any]:
    max_util = max_util_pct if max_util_pct is not None else settings.planning_threshold_pct
    result: dict[str, Any] = {
        "bandwidth_gbps": bandwidth_gbps, "protection": protection, "max_util_pct": max_util, "metric": metric,
        "primary": None, "backup": None, "diversity": None, "warnings": [], "bottlenecks": [],
    }
    if a_devices & b_devices:
        result["warnings"].append("Site A dan Site B memakai perangkat yang sama — tidak ada jalur transport.")
        return result

    base = Constraints(bandwidth_gbps, max_util, metric)
    primary = shortest_path(graph, a_devices, b_devices, base)
    if primary is None:
        _diagnose(graph, a_devices, b_devices, base, result)
        return result
    result["primary"] = describe(graph, primary, bandwidth_gbps)

    if protection == "none":
        return result
    p_links = {e.link_id for e in primary.edges}
    p_fibers = {e.fiber_id for e in primary.edges if e.fiber_id is not None}
    transit = set(primary.devices[1:-1])
    src, dst = primary.devices[0], primary.devices[-1]
    attempts = []
    if len(a_devices) > 1 or len(b_devices) > 1:
        other_a = a_devices - {src} or a_devices
        other_b = b_devices - {dst} or b_devices
        if other_a != a_devices or other_b != b_devices:
            attempts.append(("node-disjoint (perangkat ujung berbeda)", other_a, other_b, transit | ({src} if other_a != a_devices else set()) | ({dst} if other_b != b_devices else set())))
    attempts.append(("node-disjoint", a_devices, b_devices, transit))
    attempts.append(("link-disjoint", a_devices, b_devices, set()))
    for label, sa, sb, banned_nodes in attempts:
        c = Constraints(bandwidth_gbps, max_util, metric, banned_nodes=banned_nodes,
                        banned_links=p_links, banned_fibers=p_fibers)
        backup = shortest_path(graph, sa, sb, c)
        if backup is not None:
            result["backup"] = describe(graph, backup, bandwidth_gbps)
            result["diversity"] = label
            shared = set(backup.devices) & set(primary.devices)
            if label == "link-disjoint" and shared - {src, dst}:
                result["warnings"].append(
                    "Backup hanya link-disjoint: melewati perangkat yang sama dengan primary ("
                    + ", ".join(sorted(graph.hostname[d] for d in shared - {src, dst})) + ")."
                )
            break
    else:
        result["warnings"].append("Tidak ada jalur backup yang disjoint dengan kapasitas cukup — proteksi tidak terpenuhi.")
    if not p_fibers:
        result["warnings"].append("Data fiber belum terisi: risiko jalur fiber bersama (SRLG) belum bisa diperiksa.")
    return result


def _diagnose(graph: Graph, a: set[int], b: set[int], c: Constraints, result: dict[str, Any]) -> None:
    """No path within capacity: find the topological path and the links that block it."""
    loose = Constraints(c.bandwidth_gbps, c.max_util_pct, c.metric, check_capacity=False)
    path = shortest_path(graph, a, b, loose)
    if path is None:
        result["warnings"].append("Site A dan Site B tidak terhubung (tidak ada link aktif di antaranya).")
        return
    for e, frm, to in zip(path.edges, path.devices, path.devices[1:]):
        if not c.allows(e):
            need = c.bandwidth_gbps - e.available
            result["bottlenecks"].append({
                "link_id": e.link_id, "from": graph.hostname[frm], "to": graph.hostname[to],
                "capacity_gbps": e.capacity, "available_gbps": round(e.available, 3),
                "after_pct": round((e.current + e.reserved + c.bandwidth_gbps) / e.capacity * 100, 1) if e.capacity else None,
                "shortfall_gbps": round(max(need, 0), 3),
            })
    result["warnings"].append(
        f"Tidak ada jalur dengan kapasitas cukup untuk {c.bandwidth_gbps:g}G pada batas utilisasi {c.max_util_pct:g}%. "
        f"{len(result['bottlenecks'])} link di jalur terpendek perlu upgrade."
    )
    result["unconstrained_path"] = describe(graph, path, c.bandwidth_gbps)
