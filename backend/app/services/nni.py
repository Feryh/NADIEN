"""Carrier NNI ports planned as capacity objects of their own.

A hub port that reaches several sites over a carrier (PLG-NIX-BNG2-M6K-8SP smartgroup84 "NNI to XLS", 100G) carries
every site behind it. Each site's link is read on the site's own port (a 1G CSR uplink), never on the NNI, so the
link list says nothing about the NNI filling up. This module lists those ports with their own traffic history
(port_samples, written by the utilization sync and the RRD import) and the same trend and forecast as links.

An NNI is a link end that is either described as one ("NNI" in its description) or shared by several live links
(services/capacity.shared_ports), and is the wider end of at least one of them.
"""

import re
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from .. import models as m
from ..config import settings
from .capacity import (MIN_TREND_DAYS, calculate, fit_trend, is_narrower, planning_current, shared_ports,
                       spike_indexes)

_NNI_WORD = re.compile(r"\bNNI\b", re.IGNORECASE)
# A description naming a carrier path ("to PAL-PAB-BNG1-M4:Eth-Trunk12 via XL"); an own transport hop
# ("VIA-DIRECT-CORE", "VIA-HW9800-M24", "VIA-DWDM") is not a carrier.
_VIA_CARRIER = re.compile(r"\bvia\s+(?!direct\b|dwdm\b|hw9800|osn)[a-z]", re.IGNORECASE)


def _kind(description: str | None) -> str:
    """nni: a carrier NNI by its description; shared: another port several links share (a core bundle)."""
    text = description or ""
    return "nni" if _NNI_WORD.search(text) or _VIA_CARRIER.search(text) else "shared"


@dataclass
class Nni:
    interface_id: int
    device_id: int
    device: str
    port: str
    description: str | None
    speed_gbps: float
    live_gbps: float | None
    kind: str = "nni"  # nni: a carrier NNI by its description; shared: another port several links share
    links: list[dict[str, Any]] = field(default_factory=list)


def nni_ports(db: Session) -> dict[int, Nni]:
    """interface id -> NNI, with the live links (sites) behind it."""
    links = list(db.execute(select(m.Link.id, m.Link.name, m.Link.status, m.Link.a_interface_id, m.Link.b_interface_id,
                                   m.Link.capacity_gbps, m.Link.current_gbps, m.Link.a_device_id, m.Link.b_device_id)
                            .where(m.Link.status != "down")))
    shared = shared_ports((l.a_interface_id, l.b_interface_id) for l in links)
    ends = {i for l in links for i in (l.a_interface_id, l.b_interface_id) if i}
    ports = {p.id: p for p in db.execute(select(m.Interface.id, m.Interface.device_id, m.Interface.name,
                                                m.Interface.description, m.Interface.speed_gbps, m.Interface.if_type,
                                                m.Interface.in_gbps, m.Interface.out_gbps)
                                         .where(m.Interface.id.in_(ends)))}
    hosts = dict(db.execute(select(m.Device.id, m.Device.hostname)).tuples().all())
    out: dict[int, Nni] = {}
    for link in links:
        for mine, other in ((link.a_interface_id, link.b_interface_id), (link.b_interface_id, link.a_interface_id)):
            port, far = ports.get(mine), ports.get(other)
            if port is None or port.if_type not in ("physical", "bundle") or not port.speed_gbps:
                continue
            if not (_NNI_WORD.search(port.description or "") or mine in shared):
                continue
            if far is not None and not is_narrower(far.speed_gbps, port.speed_gbps):
                continue  # an equal or wider far end is a trunk between two hubs, not sites behind a carrier
            nni = out.get(mine)
            if nni is None:
                live = max(port.in_gbps or 0.0, port.out_gbps or 0.0) if port.in_gbps is not None else None
                nni = out[mine] = Nni(mine, port.device_id, hosts.get(port.device_id, "?"), port.name, port.description,
                                      port.speed_gbps, live, _kind(port.description))
            nni.links.append({"link_id": link.id, "name": link.name,
                              "far_device": hosts.get(far.device_id) if far else None,
                              "far_port": far.name if far else None, "capacity_gbps": link.capacity_gbps,
                              "current_gbps": link.current_gbps})
    # A bundle whose links all reach one box is that box's LAG seen member by member, not a port sites share.
    return {i: n for i, n in out.items() if n.kind == "nni" or len({l["far_device"] for l in n.links}) >= 2}


# --------------------------------------------------------------------------- history


def record_live(db: Session, rates: dict[int, tuple[float | None, float | None]], ts: datetime) -> int:
    """One sample per NNI port from the utilization sync's port rates (interface id -> (in, out) Gbps)."""
    count = 0
    for interface_id in nni_ports(db):
        rate = rates.get(interface_id)
        if rate is None or rate[0] is None:
            continue
        db.add(m.PortSample(interface_id=interface_id, ts=ts, in_gbps=rate[0] or 0.0, out_gbps=rate[1] or 0.0))
        count += 1
    return count


def replace_history(db: Session, interface_id: int, daily: list[tuple[datetime, float, float]],
                    first_day: datetime) -> None:
    """The RRD import's daily peaks replace whatever the port held from `first_day` on."""
    db.execute(delete(m.PortSample).where(m.PortSample.interface_id == interface_id, m.PortSample.ts >= first_day))
    db.add_all(m.PortSample(interface_id=interface_id, ts=ts, in_gbps=round(rx, 4), out_gbps=round(tx, 4))
               for ts, rx, tx in daily)


# --------------------------------------------------------------------------- capacity


def _series(db: Session, ids: list[int], since: datetime) -> dict[int, list[tuple[float, float]]]:
    out: dict[int, list[tuple[float, float]]] = defaultdict(list)
    for iface, ts, rx, tx in db.execute(select(m.PortSample.interface_id, m.PortSample.ts, m.PortSample.in_gbps,
                                               m.PortSample.out_gbps)
                                        .where(m.PortSample.interface_id.in_(ids), m.PortSample.ts >= since)
                                        .order_by(m.PortSample.ts)):
        out[iface].append((ts.timestamp() / 86400, max(rx or 0.0, tx or 0.0)))
    return out


def nni_capacity(db: Session, since: datetime) -> list[dict[str, Any]]:
    """Per NNI: capacity, current traffic, utilization, trend and forecast, and the sites behind it."""
    nnis = nni_ports(db)
    series = _series(db, list(nnis), since)
    today = time.time() / 86400
    rows = []
    for nni in nnis.values():
        points = series.get(nni.interface_id, [])
        spikes = spike_indexes(points, nni.speed_gbps)
        kept = [p for k, p in enumerate(points) if k not in spikes]
        window = [p for p in kept if p[0] >= (kept[-1][0] - settings.trend_window_days)] if kept else []
        fit = fit_trend(window)
        trend = fit if fit is not None and fit.days >= MIN_TREND_DAYS else None
        current = planning_current(points, nni.speed_gbps, today)
        if current is None:
            current = nni.live_gbps or 0.0
        result = calculate(nni.speed_gbps, current, growth_abs=fit.growth_per_month if fit else None,
                           growth_source="trend", trend=trend)
        rows.append({**result.model_dump(), "interface_id": nni.interface_id, "device_id": nni.device_id,
                     "device": nni.device, "port": nni.port, "description": nni.description, "kind": nni.kind,
                     "live_gbps": nni.live_gbps, "samples_days": round(kept[-1][0] - kept[0][0]) if len(kept) >= 2 else 0,
                     "sites": sorted(nni.links, key=lambda l: l["far_device"] or "")})
    return sorted(rows, key=lambda r: (r["months_to_threshold"] is None, r["months_to_threshold"] or 0,
                                       -r["utilization_pct"]))


def nni_of_links(db: Session) -> dict[int, str]:
    """link id -> "DEVICE PORT" of the NNI it rides, for marking site links."""
    return {link["link_id"]: f"{nni.device} {nni.port}" for nni in nni_ports(db).values() for link in nni.links}
