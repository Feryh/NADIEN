"""Topology discovery.

LLDP neighbor tables are the mandatory source. Interface descriptions taken from the
device configuration are the backup, used only for ports LLDP did not cover
(e.g. LLDP disabled on a legacy box or a neighbor across a transparent L2 path).

When both ends of an LLDP adjacency are bundle members, the link is recorded once
at bundle level with capacity = sum of member speeds.
"""

from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Device, Interface, Link, LldpNeighbor
from ..schemas import DiscoveryResult


def norm_host(name: str) -> str:
    return name.strip().split(".")[0].lower()


def norm_if(name: str) -> str:
    return name.strip().lower().replace(" ", "")


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

    def from_config(self) -> None:
        """Pair uncovered ports whose descriptions name each other's hostname."""
        hosts = sorted(self.by_host.items(), key=lambda kv: -len(kv[0]))  # longest first: PE-1 vs PE-10
        candidates = [
            i for i in self.ifaces.values()
            if i.id not in self.covered and i.parent_id is None and i.description and i.if_type != "subinterface"
        ]
        for iface in candidates:
            if iface.id in self.covered:
                continue
            desc = iface.description.lower()
            local = self.devices[iface.device_id]
            remote = next((d for h, d in hosts if h in desc and d.id != local.id), None)
            if remote is None:
                continue
            local_host = norm_host(local.hostname)
            peers = [
                p for p in candidates
                if p.device_id == remote.id and p.id not in self.covered
                and local_host in (p.description or "").lower()
            ]
            if not peers:
                continue
            # Prefer the peer whose description also names our port.
            peer = next((p for p in peers if norm_if(iface.name) in norm_if(p.description or "")), peers[0])
            self.add(iface, peer, "config")

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
    # capacity history and customer references stay intact.
    marked_down = 0
    for link in existing.values():
        if link.id not in seen_ids and link.discovery_source in ("lldp", "config") and link.status == "up":
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
        interfaces_created=run.stats["interfaces_created"],
        unresolved_neighbors=unresolved,
    )
