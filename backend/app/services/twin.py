"""Digital twin (Phase 10): what-if simulation on a copy of the network; production data is never changed.

A scenario is a list of changes:
- fail_link / fail_device / fail_pop / fail_fiber (SRLG: every link riding that fiber);
- growth: traffic +N% on the whole network or on the links touching one area;
- upgrade_link: a new capacity for an existing link; add_link: a virtual link between two devices.

Reroute model (an estimate — the real traffic matrix is not known): the traffic of a failed link between two
surviving devices moves to the shortest replacement path between its ends (fewest hops, preferring spare
capacity); with no replacement path it is lost. Traffic of links touching a failed device ends there and is lost.
Services follow their engineered paths: primary hit -> backup if intact, else down.
"""

from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models as m
from ..config import settings
from .areas import UNMAPPED_AREA, area_for_pop
from .capacity import hotspot_level

LEVELS = ("critical", "warning")


class TwinError(ValueError):
    pass


@dataclass
class _Link:
    id: int
    name: str
    a: int
    b: int
    capacity: float
    current: float
    fiber_id: int | None
    virtual: bool = False


@dataclass
class _Network:
    links: dict[int, _Link]
    hostname: dict[int, str]
    pop_of: dict[int, int]
    area_of_pop: dict[int, str]
    pop_code: dict[int, str]

    def area(self, device_id: int) -> str:
        return self.area_of_pop.get(self.pop_of.get(device_id, -1), UNMAPPED_AREA)


@dataclass
class _Applied:
    failed_links: set[int] = field(default_factory=set)
    failed_devices: set[int] = field(default_factory=set)
    factor: dict[int, float] = field(default_factory=lambda: defaultdict(lambda: 1.0))
    capacity: dict[int, float] = field(default_factory=dict)
    added: list[_Link] = field(default_factory=list)
    labels: list[str] = field(default_factory=list)


def load_network(db: Session) -> _Network:
    pops = db.execute(select(m.Pop.id, m.Pop.code, m.Pop.region, m.Pop.name)).all()
    devices = db.execute(select(m.Device.id, m.Device.hostname, m.Device.pop_id)).all()
    rows = db.execute(select(m.Link.id, m.Link.name, m.Link.a_device_id, m.Link.b_device_id, m.Link.capacity_gbps,
                             m.Link.current_gbps, m.Link.fiber_id).where(m.Link.status != "down")).all()
    return _Network(
        links={r.id: _Link(r.id, r.name, r.a_device_id, r.b_device_id, r.capacity_gbps or 0, r.current_gbps or 0,
                           r.fiber_id) for r in rows},
        hostname={d.id: d.hostname for d in devices}, pop_of={d.id: d.pop_id for d in devices},
        area_of_pop={p.id: area_for_pop(p.code, p.region, p.name) for p in pops}, pop_code={p.id: p.code for p in pops})


# --------------------------------------------------------------------------- changes


def apply_changes(net: _Network, changes: list[dict[str, Any]]) -> _Applied:
    applied = _Applied()
    for change in changes:
        kind = change["type"]
        if kind == "fail_link":
            link = _link(net, change["link_id"])
            applied.failed_links.add(link.id)
            applied.labels.append(f"Link {link.name} mati")
        elif kind == "fail_device":
            device_id = _device(net, change["device_id"])
            _fail_devices(net, applied, {device_id})
            applied.labels.append(f"Device {net.hostname[device_id]} mati")
        elif kind == "fail_pop":
            pop_id = change["pop_id"]
            if pop_id not in net.pop_code:
                raise TwinError(f"POP {pop_id} tidak ada")
            _fail_devices(net, applied, {d for d, p in net.pop_of.items() if p == pop_id})
            applied.labels.append(f"POP {net.pop_code[pop_id]} mati")
        elif kind == "fail_fiber":
            hit = {l.id for l in net.links.values() if l.fiber_id == change["fiber_id"]}
            applied.failed_links |= hit
            applied.labels.append(f"Fiber #{change['fiber_id']} putus ({len(hit)} link)")
        elif kind == "growth":
            factor = 1 + change["pct"] / 100
            area = change.get("area")
            for link in net.links.values():
                if area is None or area in (net.area(link.a), net.area(link.b)):
                    applied.factor[link.id] *= factor
            applied.labels.append(f"Trafik {change['pct']:+g}% di {area or 'seluruh jaringan'}")
        elif kind == "upgrade_link":
            link = _link(net, change["link_id"])
            applied.capacity[link.id] = change["capacity_gbps"]
            applied.labels.append(f"Upgrade {link.name}: {link.capacity:g}G → {change['capacity_gbps']:g}G")
        elif kind == "add_link":
            a, b = _device(net, change["a_device_id"]), _device(net, change["b_device_id"])
            if a == b:
                raise TwinError("Link virtual butuh dua device berbeda")
            name = change.get("name") or f"{net.hostname[a]} ↔ {net.hostname[b]} (virtual)"
            applied.added.append(_Link(-(len(applied.added) + 1), name, a, b, change["capacity_gbps"], 0, None, True))
            applied.labels.append(f"Link baru {name} {change['capacity_gbps']:g}G")
        else:
            raise TwinError(f"Jenis perubahan tidak dikenal: {kind}")
    return applied


def _link(net: _Network, link_id: int) -> _Link:
    link = net.links.get(link_id)
    if link is None:
        raise TwinError(f"Link {link_id} tidak ada atau sudah down")
    return link


def _device(net: _Network, device_id: int) -> int:
    if device_id not in net.hostname:
        raise TwinError(f"Device {device_id} tidak ada")
    return device_id


def _fail_devices(net: _Network, applied: _Applied, devices: set[int]) -> None:
    applied.failed_devices |= devices
    applied.failed_links |= {l.id for l in net.links.values() if l.a in devices or l.b in devices}


# --------------------------------------------------------------------------- graph


def _adjacency(links: list[_Link], spare: dict[int, float]) -> dict[int, list[tuple[int, int]]]:
    """device -> [(neighbour, link id)], most spare capacity first so BFS ties pick the roomiest link."""
    adj: dict[int, list[tuple[int, int]]] = defaultdict(list)
    for link in sorted(links, key=lambda l: -spare.get(l.id, 0)):
        adj[link.a].append((link.b, link.id))
        adj[link.b].append((link.a, link.id))
    return adj


def _shortest(adj: dict[int, list[tuple[int, int]]], src: int, dst: int) -> list[int] | None:
    parent: dict[int, tuple[int, int] | None] = {src: None}
    queue = deque([src])
    while queue:
        node = queue.popleft()
        if node == dst:
            path: list[int] = []
            while parent[node] is not None:
                prev, link_id = parent[node]  # type: ignore[misc]
                path.append(link_id)
                node = prev
            return path[::-1]
        for neighbour, link_id in adj.get(node, []):
            if neighbour not in parent:
                parent[neighbour] = (node, link_id)
                queue.append(neighbour)
    return None


def _main_component(adj: dict[int, list[tuple[int, int]]], nodes: set[int]) -> set[int]:
    seen: set[int] = set()
    best: set[int] = set()
    for start in nodes:
        if start in seen:
            continue
        component = {start}
        queue = deque([start])
        while queue:
            for neighbour, _ in adj.get(queue.popleft(), []):
                if neighbour not in component:
                    component.add(neighbour)
                    queue.append(neighbour)
        seen |= component
        if len(component) > len(best):
            best = component
    return best


# --------------------------------------------------------------------------- simulation


def simulate(db: Session, changes: list[dict[str, Any]]) -> dict[str, Any]:
    net = load_network(db)
    applied = apply_changes(net, changes)
    links = list(net.links.values()) + applied.added
    capacity = {l.id: applied.capacity.get(l.id, l.capacity) for l in links}
    load = {l.id: l.current * applied.factor[l.id] for l in links}
    surviving = [l for l in links if l.id not in applied.failed_links]
    spare = {l.id: capacity[l.id] - load[l.id] for l in surviving}
    adj = _adjacency(surviving, spare)

    rerouted = lost_unroutable = lost_terminating = 0.0
    unroutable: list[dict[str, Any]] = []
    paths: dict[tuple[int, int], list[int] | None] = {}
    for link in (net.links[i] for i in sorted(applied.failed_links)):
        traffic = load[link.id]
        if link.a in applied.failed_devices or link.b in applied.failed_devices:
            lost_terminating += traffic
            continue
        key = (min(link.a, link.b), max(link.a, link.b))
        if key not in paths:
            paths[key] = _shortest(adj, link.a, link.b)
        path = paths[key]
        if path is None:
            lost_unroutable += traffic
            unroutable.append({"link": link.name, "traffic_gbps": round(traffic, 2)})
            continue
        rerouted += traffic
        for link_id in path:
            load[link_id] += traffic

    before_adj = _adjacency(list(net.links.values()), {})
    nodes = set(net.hostname)
    main_before = _main_component(before_adj, nodes)
    main_after = _main_component(adj, nodes - applied.failed_devices)
    isolated = sorted(main_before - main_after - applied.failed_devices)

    return {
        "changes": applied.labels,
        "totals": {"failed_links": len(applied.failed_links), "failed_devices": len(applied.failed_devices),
                   "rerouted_gbps": round(rerouted, 2), "lost_unroutable_gbps": round(lost_unroutable, 2),
                   "lost_terminating_gbps": round(lost_terminating, 2), "isolated_devices": len(isolated)},
        **_link_impact(net, links, applied, capacity, load),
        "isolated": [{"hostname": net.hostname[d], "area": net.area(d)} for d in isolated[:50]],
        "unroutable": sorted(unroutable, key=lambda u: -u["traffic_gbps"])[:30],
        "services": _service_impact(db, applied),
        "notes": [
            "Perkiraan: trafik link yang mati dipindah ke jalur terpendek pengganti antar kedua ujungnya; "
            "pola trafik end-to-end yang sebenarnya tidak diketahui.",
            "Trafik link yang menyentuh device/POP yang mati dianggap berakhir di sana (hilang).",
            "Link berstatus down tidak ikut dihitung; data produksi tidak diubah.",
        ],
    }


def _level(pct: float) -> str:
    return hotspot_level(pct)


def _after_level(pct: float) -> str:
    """In a simulation an extreme value is overload caused by the scenario, not bad data: no "suspect" level."""
    if pct >= settings.hotspot_critical_pct:
        return "critical"
    return "warning" if pct >= settings.hotspot_warning_pct else "normal"


def _link_impact(net: _Network, links: list[_Link], applied: _Applied, capacity: dict[int, float],
                 load: dict[int, float]) -> dict[str, Any]:
    rows = []
    counts = {"before": dict.fromkeys(LEVELS, 0), "after": dict.fromkeys(LEVELS, 0)}
    areas: dict[str, dict[str, float]] = defaultdict(lambda: {"before_critical": 0, "after_critical": 0,
                                                             "before_max_pct": 0.0, "after_max_pct": 0.0})
    for link in links:
        failed = link.id in applied.failed_links  # counted in "before", gone "after"
        before = link.current / link.capacity * 100 if link.capacity > 0 and not link.virtual else None
        after = load[link.id] / capacity[link.id] * 100 if capacity[link.id] > 0 and not failed else None
        level_before = _level(before) if before is not None else None
        # A link whose own data is already implausible stays out of the comparison on both sides.
        level_after = None if after is None or level_before == "suspect" else _after_level(after)
        for side, level in (("before", level_before), ("after", level_after)):
            if level in LEVELS:
                counts[side][level] += 1
        for area in {net.area(link.a), net.area(link.b)}:
            a = areas[area]
            if before is not None and level_before != "suspect":
                a["before_max_pct"] = max(a["before_max_pct"], before)
                a["before_critical"] += level_before == "critical"
            if level_after is not None:
                a["after_max_pct"] = max(a["after_max_pct"], after)
                a["after_critical"] += level_after == "critical"
        changed = link.virtual or link.id in applied.capacity or abs(load[link.id] - link.current) > 1e-6
        if not failed and changed and level_after is not None:
            rows.append({"link_id": link.id if not link.virtual else None, "name": link.name,
                         "a_device": net.hostname.get(link.a), "b_device": net.hostname.get(link.b),
                         "capacity_gbps": capacity[link.id], "before_pct": round(before, 1) if before is not None else None,
                         "after_pct": round(after, 1), "load_gbps": round(load[link.id], 2), "level": level_after,
                         "virtual": link.virtual})
    rows.sort(key=lambda r: -r["after_pct"])
    return {"link_levels": counts, "links": rows[:40], "links_changed": len(rows),
            "areas": sorted(({"area": k, **{f: round(v, 1) for f, v in a.items()}} for k, a in areas.items()),
                            key=lambda a: -a["after_max_pct"])}


def _service_impact(db: Session, applied: _Applied) -> dict[str, Any]:
    paths: dict[int, dict[str, m.ServicePath]] = defaultdict(dict)
    for p in db.scalars(select(m.ServicePath).where(m.ServicePath.released.is_not(True))):
        paths[p.service_id][p.role] = p

    def broken(path: m.ServicePath | None) -> bool:
        return path is None or bool(set(path.link_ids or []) & applied.failed_links) \
            or bool(set(path.device_ids or []) & applied.failed_devices)

    impact: dict[str, list[dict[str, Any]]] = {"down": [], "on_backup": [], "unprotected": []}
    for service in db.scalars(select(m.Service).where(m.Service.status != "terminated")):
        row = {"service_id": service.id, "circuit_id": service.circuit_id, "service_type": service.service_type,
               "bandwidth_mbps": service.bandwidth_mbps}
        own = paths.get(service.id, {})
        primary, backup = own.get("primary"), own.get("backup")
        if {service.a_device_id, service.b_device_id} & applied.failed_devices:
            impact["down"].append({**row, "reason": "perangkat ujung mati"})
        elif primary is not None and broken(primary):
            if backup is not None and not broken(backup):
                impact["on_backup"].append({**row, "reason": "jalur utama terputus, pindah ke backup"})
            else:
                impact["down"].append({**row, "reason": "jalur utama terputus, tanpa backup yang utuh"})
        elif backup is not None and broken(backup):
            impact["unprotected"].append({**row, "reason": "backup terputus, layanan tanpa proteksi"})
    return {**{k: v[:100] for k, v in impact.items()}, "counts": {k: len(v) for k, v in impact.items()}}
