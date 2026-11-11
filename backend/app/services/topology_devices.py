"""Device topology with filters: only devices and links on the drawing, the area / province / regency / POP chosen in
filters instead of drawn as boxes. A link leaving the chosen scope ends on the far device itself, marked `outside`."""

from collections import Counter, defaultdict
from typing import Any

from fastapi import HTTPException

from .topology_readmodel import (
    Agg,
    Ctx,
    TopologyLinks,
    device_node,
    edges_of,
    pair,
    path_fields,
    place_devices,
    segment_of,
    segments_of,
)
from .areas import ROLE_TIER

# Beyond this the drawing is a hairball and the free layout takes too long: the filters must narrow it first.
MAX_DEVICES = 1500


def _in_scope(ctx: Ctx, pop_id: int, area: str | None, province: str | None, regency: str | None,
              pop: str | None) -> bool:
    path_area, path_province, path_regency = ctx.path_of_pop(pop_id)
    code = ctx.pops[pop_id].code if pop_id in ctx.pops else ""
    return ((area is None or path_area == area) and (province is None or path_province == province)
            and (regency is None or path_regency == regency) and (pop is None or code.upper() == pop.upper()))


def devices_view(ctx: Ctx, links: TopologyLinks, area: str | None, province: str | None, regency: str | None,
                 pop: str | None) -> dict[str, Any]:
    local = {d.id: d for d in ctx.devices.values() if _in_scope(ctx, d.pop_id, area, province, regency, pop)}
    if len(local) > MAX_DEVICES:
        raise HTTPException(422, f"{len(local)} perangkat dalam filter ini, lebih dari {MAX_DEVICES}: "
                                 "pilih provinsi, kab/kota atau POP untuk mempersempitnya")
    edges: dict[tuple[str, str, str], Agg] = defaultdict(Agg)
    totals: dict[str, Agg] = defaultdict(Agg)
    outside: set[int] = set()
    for link in links.shown:
        a_in, b_in = link.a_device_id in local, link.b_device_id in local
        if not (a_in or b_in) or link.a_device_id not in ctx.devices or link.b_device_id not in ctx.devices:
            continue
        segment = segment_of(ctx, link)
        totals[segment].add(link)
        edges[(*pair(f"dev:{link.a_device_id}", f"dev:{link.b_device_id}"), segment)].add(link)
        outside.update(d for d, inside in ((link.a_device_id, a_in), (link.b_device_id, b_in)) if not inside)

    by_tier = lambda d: (ROLE_TIER.get(d.role, 4), d.hostname)  # noqa: E731
    nodes = [device_node(ctx, d) for d in sorted(local.values(), key=by_tier)]
    for d in sorted((ctx.devices[i] for i in outside), key=by_tier):
        # Where the far device sits is shown on it (details panel, tooltip), never as a box on the drawing.
        nodes.append({**device_node(ctx, d), "outside": True, **path_fields(ctx.path_of_pop(d.pop_id))})
    place_devices(ctx, nodes)
    return {"level": "devices", "area": area, "province": province, "regency": regency, "pop": pop,
            "nodes": nodes, "edges": edges_of(edges), "segments": segments_of(totals), "outside_devices": len(outside),
            "hidden_links": links.hidden_count(lambda link: link.a_device_id in local or link.b_device_id in local)}


def scopes(ctx: Ctx) -> list[dict[str, Any]]:
    """Every POP with devices and where it sits: what the area / province / regency / POP filters offer."""
    count = Counter(d.pop_id for d in ctx.devices.values())
    rows = []
    for pop_id, devices in count.items():
        if pop_id not in ctx.pops:
            continue
        rows.append({**path_fields(ctx.path_of_pop(pop_id)), "pop": ctx.pops[pop_id].code,
                     "pop_name": ctx.pops[pop_id].name, "devices": devices})
    return sorted(rows, key=lambda r: (r["area"], r["province"], r["regency"], r["pop"]))
