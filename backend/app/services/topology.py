"""Topology discovery.

LLDP neighbor tables are the mandatory source. Interface descriptions taken from the
device configuration are the backup, used only for ports LLDP did not cover
(e.g. LLDP disabled on a legacy box or a neighbor across a transparent L2 path).

When both ends of an LLDP adjacency are bundle members, the link is recorded once
at bundle level with capacity = sum of member speeds.
"""

import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from ..models import Device, Interface, Link, LldpNeighbor, UtilizationSample
from ..schemas import DiscoveryResult


def norm_host(name: str) -> str:
    return name.strip().split(".")[0].lower()


def norm_if(name: str) -> str:
    return name.strip().lower().replace(" ", "")


_SPLIT = re.compile(r"[^a-z0-9]+")


def host_key(name: str) -> str:
    """'JKT_P_01.metro.net' -> 'jkt-p-01': the form used to spot hostnames inside descriptions."""
    return "-".join(p for p in _SPLIT.split(norm_host(name)) if p)


@dataclass
class _Adjacency:
    a: Interface
    b: Interface
    link_type: str
    source: str
    member_pairs: set[frozenset[int]] = field(default_factory=set)


class _Discovery:
    def __init__(self, db: Session):
        self.db = db
        self.stats: Counter[str] = Counter()
        self.devices = {d.id: d for d in db.scalars(select(Device))}
        self.by_host = {norm_host(d.hostname): d for d in self.devices.values()}
        self.by_host_key = {host_key(d.hostname): d for d in self.devices.values()}
        self.host_first_parts = {k.split("-")[0] for k in self.by_host_key}
        self.max_host_parts = max((k.count("-") + 1 for k in self.by_host_key), default=1)
        self.ifaces = {i.id: i for i in db.scalars(select(Interface))}
        self.if_index = {(i.device_id, norm_if(i.name)): i for i in self.ifaces.values()}
        self.adjacencies: dict[frozenset[int], _Adjacency] = {}
        self.covered: set[int] = set()

    def iface(self, device: Device, name: str) -> Interface:
        key = (device.id, norm_if(name))
        found = self.if_index.get(key)
        if found is None:
            found = Interface(device_id=device.id, name=name.strip(), if_type="physical", oper_status="up")
            self.db.add(found)
            self.db.flush()
            self.if_index[key] = found
            self.ifaces[found.id] = found
            self.stats["interfaces_created"] += 1
        return found

    def add(self, a: Interface, b: Interface, source: str) -> None:
        member_pair = frozenset((a.id, b.id))
        if a.parent_id and b.parent_id:
            a, b, link_type = self.ifaces[a.parent_id], self.ifaces[b.parent_id], "bundle"
        else:
            link_type = "metro_e"
        key = frozenset((a.id, b.id))
        adj = self.adjacencies.setdefault(key, _Adjacency(a, b, link_type, source))
        adj.member_pairs.add(member_pair)
        self.covered.update(member_pair)
        self.covered.update(key)

    def from_lldp(self) -> list[str]:
        unresolved = []
        for n in self.db.scalars(select(LldpNeighbor)):
            local = self.devices.get(n.device_id)
            remote = self.by_host.get(norm_host(n.remote_system_name))
            if local is None or remote is None:
                unresolved.append(f"{local.hostname if local else n.device_id}:{n.local_interface} -> {n.remote_system_name}")
                continue
            self.add(self.iface(local, n.local_interface), self.iface(remote, n.remote_port_id), "lldp")
        return unresolved

    def mentioned_device(self, description: str) -> Device | None:
        """Device whose hostname appears in a description, longest match wins (PE-1 vs PE-10).

        Hostnames and descriptions are both reduced to '-'-joined alphanumeric parts, so
        "TO JKT_P_01 Hu0/0/0/1" finds JKT-P-01. Cost is linear in the description length.
        """
        parts = [p for p in _SPLIT.split(description.lower()) if p]
        best: tuple[int, Device] | None = None
        for i, first in enumerate(parts):
            if first not in self.host_first_parts:
                continue
            for j in range(min(len(parts), i + self.max_host_parts), i, -1):
                device = self.by_host_key.get("-".join(parts[i:j]))
                if device is not None:
                    if best is None or j - i > best[0]:
                        best = (j - i, device)
                    break
        return best[1] if best else None

    def from_config(self) -> None:
        """Pair uncovered ports whose descriptions name each other's hostname.

        Only for device pairs LLDP saw no adjacency between at all: otherwise a bundle described
        "TO-X" would be paired on top of its LLDP-discovered members and counted twice.
        Ports are grouped by device pair first, so the work is linear in the number of ports.
        """
        lldp_pairs = {frozenset((adj.a.device_id, adj.b.device_id)) for adj in self.adjacencies.values()}
        by_pair: dict[frozenset[int], dict[int, list[Interface]]] = {}
        for iface in self.ifaces.values():
            if iface.id in self.covered or iface.parent_id or not iface.description or iface.if_type == "subinterface":
                continue
            remote = self.mentioned_device(iface.description)
            if remote is None or remote.id == iface.device_id:
                continue
            pair = frozenset((iface.device_id, remote.id))
            if pair in lldp_pairs:
                continue
            by_pair.setdefault(pair, {}).setdefault(iface.device_id, []).append(iface)

        for sides in by_pair.values():
            if len(sides) != 2:
                continue  # only one end describes the other: not enough to call it a link
            (_, ours), (_, theirs) = sides.items()
            remaining = list(theirs)
            for a in ours:
                if not remaining:
                    break
                # Prefer the peer whose description names our port (or ours names theirs).
                b = next((p for p in remaining if norm_if(a.name) in norm_if(p.description or "")), None) or next(
                    (p for p in remaining if norm_if(p.name) in norm_if(a.description or "")), remaining[0]
                )
                remaining.remove(b)
                self.add(a, b, "config")

    def capacity(self, adj: _Adjacency) -> float | None:
        speeds = []
        for pair in adj.member_pairs:
            ends = [self.ifaces[i].speed_gbps for i in pair]
            if any(s is None for s in ends):
                return None
            speeds.append(min(ends))
        return sum(speeds) if speeds else None


def discover_links(db: Session) -> DiscoveryResult:
    run = _Discovery(db)
    unresolved = run.from_lldp()
    run.from_config()

    now = datetime.now(timezone.utc)
    existing = {
        frozenset((l.a_interface_id, l.b_interface_id)): l
        for l in db.scalars(select(Link))
        if l.a_interface_id and l.b_interface_id
    }
    seen_ids: set[int] = set()
    created = updated = 0

    for key, adj in run.adjacencies.items():
        a_dev, b_dev = run.devices[adj.a.device_id], run.devices[adj.b.device_id]
        cap = run.capacity(adj)
        up = all(run.ifaces[i].oper_status == "up" for i in key) if adj.source == "config" else True
        link = existing.get(key)
        if link is None:
            link = Link(
                name=f"{a_dev.hostname}:{adj.a.name} <> {b_dev.hostname}:{adj.b.name}",
                a_device_id=a_dev.id,
                a_interface_id=adj.a.id,
                b_device_id=b_dev.id,
                b_interface_id=adj.b.id,
                capacity_gbps=cap or 0,
            )
            db.add(link)
            created += 1
        else:
            updated += 1
            if cap:
                link.capacity_gbps = cap
        link.link_type = adj.link_type
        link.status = "up" if up else "down"
        link.last_seen = now
        if link.discovery_source != "manual":
            link.discovery_source = adj.source
        db.flush()
        seen_ids.add(link.id)

    # Discovered links that disappeared from LLDP/config are flagged, not deleted —
    # capacity history and customer references stay intact. The exception is a member-level link
    # whose ports now belong to a discovered bundle: it was superseded, not lost.
    member_pairs = {pair for adj in run.adjacencies.values() if adj.link_type == "bundle" for pair in adj.member_pairs}
    marked_down = superseded = 0
    for key, link in existing.items():
        if link.id in seen_ids or link.discovery_source not in ("lldp", "config"):
            continue
        if key in member_pairs:
            db.execute(delete(UtilizationSample).where(UtilizationSample.link_id == link.id))
            db.delete(link)
            superseded += 1
        elif link.status == "up":
            link.status = "down"
            marked_down += 1

    db.commit()
    sources = Counter(adj.source for adj in run.adjacencies.values())
    return DiscoveryResult(
        lldp_links=sources["lldp"],
        config_links=sources["config"],
        created=created,
        updated=updated,
        marked_down=marked_down,
        superseded=superseded,
        interfaces_created=run.stats["interfaces_created"],
        unresolved_neighbors=unresolved,
    )
