"""The city topology view: every device of one kabupaten/kota with one entry per physical link.

Unlike the aggregated levels, parallel links stay separate here so the map can draw and colour each cable.
"""

from collections import defaultdict
from collections.abc import Callable, Iterable
from typing import Any

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models as m
from . import cache
from .areas import ROLE_TIER
from .capacity import capacity_color
from .segments import link_segment
from .topology_readmodel import (
    Agg,
    Ctx,
    device_node,
    edges_of,
    load_links,
    pair,
    path_fields,
    place_devices,
    pop_labels,
    segments_of,
)
from .topology_structure import Structure, analyse

# Above this many devices in a city the access tier (3) is drawn as one node per POP, so the map stays usable.
SUMMARY_THRESHOLD = 600
ACCESS_TIER = 3
CLUSTER_TIERS = (2, 3)  # metro and access rings; core/edge redundancy is not a ring to draw
_IN_CHUNK = 500  # keeps IN (...) lists well under every database's bind-parameter limit


def network_structure(db: Session) -> Structure:
    """Rings, meshes and SPOFs over the whole link graph: a view that shows only part of the network must not
    invent single points of failure."""
    return cache.cached(("topology-structure",),
                        lambda: analyse((link.id, link.a_device_id, link.b_device_id) for link in load_links(db)))


def _chunks(values: list[int]) -> Iterable[list[int]]:
    for start in range(0, len(values), _IN_CHUNK):
        yield values[start:start + _IN_CHUNK]


def _link_rows(db: Session, link_ids: set[int]) -> list[Any]:
    rows: list[Any] = []
    for chunk in _chunks(sorted(link_ids)):
        rows.extend(db.execute(
            select(m.Link.id, m.Link.a_device_id, m.Link.b_device_id, m.Link.a_interface_id, m.Link.b_interface_id,
                   m.Link.capacity_gbps, m.Link.current_gbps, m.Link.reserved_gbps, m.Link.status)
            .where(m.Link.id.in_(chunk))))
    return sorted(rows, key=lambda row: row.id)


def _interface_names(db: Session, interface_ids: set[int]) -> dict[int, str]:
    names: dict[int, str] = {}
    for chunk in _chunks(sorted(interface_ids)):
        names.update(db.execute(select(m.Interface.id, m.Interface.name).where(m.Interface.id.in_(chunk))).all())
    return names


def link_entries(db: Session, ctx: Ctx, link_ids: set[int], node_of: Callable[[int], str],
                 structure: Structure) -> list[dict]:
    """One dict per link record. `node_of` maps a device id to the node drawn for it; the real hostnames and
    interfaces stay in the entry even when a node stands for many devices."""
    rows = _link_rows(db, link_ids)
    names = _interface_names(db, {i for row in rows for i in (row.a_interface_id, row.b_interface_id) if i})
    entries = []
    for row in rows:
        a, b = ctx.devices[row.a_device_id], ctx.devices[row.b_device_id]
        capacity = row.capacity_gbps or 0
        current = row.current_gbps or 0
        committed = (current + (row.reserved_gbps or 0)) / capacity * 100 if capacity else 0.0
        entries.append({
            "id": row.id, "source": node_of(a.id), "target": node_of(b.id),
            "a_device": a.hostname, "a_interface": names.get(row.a_interface_id),
            "b_device": b.hostname, "b_interface": names.get(row.b_interface_id),
            "capacity_gbps": round(capacity, 1), "current_gbps": round(current, 1),
            "utilization_pct": round(current / capacity * 100, 1) if capacity else 0.0,
            "committed_pct": round(committed, 1), "color": capacity_color(committed),
            "status": row.status, "segment": link_segment(a.role, b.role), "spof": row.id in structure.spof_links,
        })
    return entries


def _city_pops(ctx: Ctx, path: tuple[str, str, str]) -> set[int]:
    pops = {pid for pid in ctx.pops if ctx.path_of_pop(pid) == path}
    if not pops:
        raise HTTPException(404, f"No POPs in {' / '.join(path)}")
    return pops


def _summarized(ctx: Ctx, local: dict[int, Any]) -> dict[int, list[int]]:
    """Access devices per POP that collapse into a summary node; empty when the city is small enough."""
    if len(local) <= SUMMARY_THRESHOLD:
        return {}
    grouped: dict[int, list[int]] = defaultdict(list)
    for device in local.values():
        if ROLE_TIER.get(device.role, 4) == ACCESS_TIER:
            grouped[device.pop_id].append(device.id)
    return {pop_id: sorted(ids) for pop_id, ids in grouped.items()}


def _clusters(ctx: Ctx, structure: Structure, local: dict[int, Any], hidden: set[int]) -> list[dict]:
    clusters = []
    for cluster in sorted(structure.clusters, key=lambda c: c.id):
        members = cluster.device_ids
        if not members <= local.keys() or members & hidden:
            continue
        if any(ROLE_TIER.get(local[i].role, 4) not in CLUSTER_TIERS for i in members):
            continue
        pops = {ctx.pop_code(i) for i in members}
        clusters.append({"id": cluster.id, "kind": cluster.kind, "pop": next(iter(pops)) if len(pops) == 1 else None,
                         "device_ids": sorted(members)})
    return clusters


def _device_nodes(ctx: Ctx, local: dict[int, Any], hidden: set[int], city: str, structure: Structure,
                  cluster_ids: set[str]) -> list[dict]:
    nodes = []
    for device in sorted(local.values(), key=lambda d: (ROLE_TIER.get(d.role, 4), d.hostname)):
        if device.id in hidden:
            continue
        cluster_id = structure.cluster_of.get(device.id)
        nodes.append({**device_node(ctx, device), "city": city,
                      "cluster_id": cluster_id if cluster_id in cluster_ids else None,
                      "spof": device.id in structure.spof_devices})
    return nodes


def _summary_nodes(ctx: Ctx, summarized: dict[int, list[int]]) -> tuple[list[dict], list[dict]]:
    nodes, described = [], []
    for pop_id, ids in sorted(summarized.items(), key=lambda item: ctx.pops[item[0]].code):
        pop = ctx.pops[pop_id].code
        nodes.append({"id": f"summary:{pop}", "kind": "summary", "pop": pop, "label": f"{len(ids)} akses",
                      "devices": len(ids), "tier": ACCESS_TIER})
        described.append({"node_id": f"summary:{pop}", "pop": pop, "count": len(ids), "device_ids": ids})
    return nodes, described


def _external_nodes(ctx: Ctx, remote: dict[str, set[int]]) -> list[dict]:
    by_code = {p.code: p for p in ctx.pops.values()}
    labels = pop_labels(ctx, {by_code[code].id for code in remote if code in by_code})
    return [{"id": f"pop:{code}", "kind": "external_pop", "code": code, "devices": len(devices),
             "label": labels.get(by_code[code].id, code) if code in by_code else code,
             **(path_fields(ctx.path_of_pop(by_code[code].id)) if code in by_code else {})}
            for code, devices in sorted(remote.items())]


def _pop_entries(ctx: Ctx, pop_ids: set[int], local: dict[int, Any], regency: str) -> list[dict]:
    count: dict[int, int] = defaultdict(int)
    for device in local.values():
        count[device.pop_id] += 1
    return [{"code": ctx.pops[pid].code, "name": ctx.pops[pid].name,
             "city": ctx.place_of_pop[pid].city or regency, "device_count": count[pid]}
            for pid in sorted(pop_ids, key=lambda pid: ctx.pops[pid].code)]


def _collect_links(db: Session, ctx: Ctx, local: dict[int, Any], node_of: Callable[[int], str]):
    """Ids of every link with an end in the city, aggregates for edges/segments and the far-end devices per POP."""
    link_ids: set[int] = set()
    edges: dict[tuple[str, str, str], Agg] = defaultdict(Agg)
    totals: dict[str, Agg] = defaultdict(Agg)
    remote: dict[str, set[int]] = defaultdict(set)
    for link in load_links(db):
        if link.a_device_id not in ctx.devices or link.b_device_id not in ctx.devices:
            continue
        if link.a_device_id not in local and link.b_device_id not in local:
            continue
        link_ids.add(link.id)
        segment = link_segment(ctx.devices[link.a_device_id].role, ctx.devices[link.b_device_id].role)
        totals[segment].add(link)
        source, target = node_of(link.a_device_id), node_of(link.b_device_id)
        if source != target:
            edges[(*pair(source, target), segment)].add(link)
        for device_id in (link.a_device_id, link.b_device_id):
            if device_id not in local:
                remote[ctx.pop_code(device_id)].add(device_id)
    return link_ids, edges, totals, remote


def city_view(db: Session, ctx: Ctx, area: str, province: str, regency: str) -> dict:
    path = (area, province, regency)
    pop_ids = _city_pops(ctx, path)
    local = {d.id: d for d in ctx.devices.values() if d.pop_id in pop_ids}
    summarized = _summarized(ctx, local)
    hidden = {i for ids in summarized.values() for i in ids}
    structure = network_structure(db)

    def node_of(device_id: int) -> str:
        if device_id not in local:
            return f"pop:{ctx.pop_code(device_id)}"
        return f"summary:{ctx.pop_code(device_id)}" if device_id in hidden else f"dev:{device_id}"

    link_ids, edges, totals, remote = _collect_links(db, ctx, local, node_of)
    clusters = _clusters(ctx, structure, local, hidden)
    nodes = _device_nodes(ctx, local, hidden, regency, structure, {c["id"] for c in clusters})
    summary_nodes, described = _summary_nodes(ctx, summarized)
    nodes += summary_nodes + _external_nodes(ctx, remote)
    place_devices(ctx, nodes)
    return {"level": "city", **path_fields(path), "nodes": nodes, "pops": _pop_entries(ctx, pop_ids, local, regency),
            "clusters": clusters, "links": link_entries(db, ctx, link_ids, node_of, structure),
            "edges": edges_of(edges), "segments": segments_of(totals), "summarized": described}
