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
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from ..models import Device, Interface, Link, LldpNeighbor, TopologyEvent, UtilizationSample
from ..schemas import DiscoveryResult
from .areas import csr_parts


_IP = re.compile(r"[\d.]+|[0-9a-fA-F:]*:[0-9a-fA-F:]*")


def strip_domain(name: str) -> str:
    """'jkt-p-01.metro.net' -> 'jkt-p-01', but 'R6.BAM.LEAF-CNDC.02' stays whole.

    Only a trailing alphabetic label (a TLD such as .net / .id) marks an FQDN; dotted hostnames
    like R6.BAM.LEAF-CNDC.02 would otherwise all collapse to 'R6'.
    """
    name = name.strip()
    if "." not in name or _IP.fullmatch(name):
        return name
    last = name.rsplit(".", 1)[1]
    return name.split(".", 1)[0] if last.isalpha() and 2 <= len(last) <= 24 else name


def norm_host(name: str) -> str:
    return strip_domain(name).lower()


def legacy_host_key(name: str) -> str:
    """Key used before dotted hostnames were kept whole (everything before the first dot)."""
    return name.strip().split(".")[0].lower()


def norm_if(name: str) -> str:
    return name.strip().lower().replace(" ", "")


_SPLIT = re.compile(r"[^a-z0-9]+")


def host_key(name: str) -> str:
    """'JKT_P_01.metro.net' -> 'jkt-p-01': the form used to spot hostnames inside descriptions."""
    return "-".join(p for p in _SPLIT.split(norm_host(name)) if p)


def _names_port(text: str, name: str) -> bool:
    """`name` appears in `text` as a whole port name: not inside a longer name (gei- in xgei-) and
    not followed by more digits (…/0/1 in …/0/10)."""
    start = text.find(name)
    while start != -1:
        before = text[start - 1] if start else ""
        after = text[start + len(name)] if start + len(name) < len(text) else ""
        if not before.isalnum() and not after.isdigit():
            return True
        start = text.find(name, start + 1)
    return False


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
        self._add_site_keys()
        self.host_first_parts = {k.split("-")[0] for k in self.by_host_key}
        self.max_host_parts = max((k.count("-") + 1 for k in self.by_host_key), default=1)
        self.ifaces = {i.id: i for i in db.scalars(select(Interface))}
        self.if_index = {(i.device_id, norm_if(i.name)): i for i in self.ifaces.values()}
        self.adjacencies: dict[frozenset[int], _Adjacency] = {}
        self.covered: set[int] = set()
        self.created_ids: set[int] = set()
        self._ports_by_device: dict[int, list[Interface]] | None = None

    def _add_site_keys(self) -> None:
        """Cell-site neighbours are often written without the role/vendor suffix
        ("SUL-SN-SGM-0011:xgei-1/1/0/1" for SUL-SN-SGM-0011-ME1-ZTE). REGION-PROV-CITY-SITE is unique
        per site, so it is accepted as a name too — only when exactly one device has it."""
        sites: dict[str, list[Device]] = {}
        for device in self.devices.values():
            if csr_parts(device.hostname):
                sites.setdefault("-".join(host_key(device.hostname).split("-")[:4]), []).append(device)
        for key, devices in sites.items():
            if len(devices) == 1 and key not in self.by_host_key:
                self.by_host_key[key] = devices[0]

    def iface(self, device: Device, name: str) -> Interface:
        key = (device.id, norm_if(name))
        found = self.if_index.get(key)
        if found is None:
            found = Interface(device_id=device.id, name=name.strip(), if_type="physical", oper_status="up")
            self.db.add(found)
            self.db.flush()
            self.if_index[key] = found
            self.ifaces[found.id] = found
            self.created_ids.add(found.id)
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

        for pair, sides in by_pair.items():
            if len(sides) != 2:
                self.one_sided(pair, sides)
                continue
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

    def one_sided(self, pair: frozenset[int], sides: dict[int, list[Interface]]) -> None:
        """Only one end describes the other (typical for a cell-site uplink to a metro box that is
        not described back). Accepted only when the description also names an existing, uncovered
        port of the remote device — the hostname alone is not enough to call it a link."""
        ((device_id, ours),) = sides.items()
        remote_id = next(iter(pair - {device_id}))
        for a in ours:
            b = self.named_port(remote_id, a.description or "")
            if b is not None and b.id not in self.covered:
                self.add(a, b, "config")

    def named_port(self, device_id: int, description: str) -> Interface | None:
        """Port of `device_id` written in the description, longest name wins (xgei-1/1/0/10 over /1)."""
        if self._ports_by_device is None:
            self._ports_by_device = {}
            for iface in self.ifaces.values():
                if iface.if_type != "subinterface":
                    self._ports_by_device.setdefault(iface.device_id, []).append(iface)
        text = norm_if(description)
        best: Interface | None = None
        for iface in self._ports_by_device.get(device_id, []):
            name = norm_if(iface.name)
            if len(name) >= 4 and _names_port(text, name) and (best is None or len(name) > len(norm_if(best.name))):
                best = iface
        return best

    def phantom_ids(self, valid_iface_ids: set[int] | None) -> set[int]:
        """Ports the NMS does not know that carry nothing but a name — what discovery created
        from LLDP port names that matched no real port. Never config-imported or edited ports."""
        if valid_iface_ids is None:
            return set()
        # Only devices the NMS actually reported on; hand-made devices keep their bare ports.
        synced_devices = {self.ifaces[i].device_id for i in valid_iface_ids if i in self.ifaces}
        return {
            i.id for i in self.ifaces.values()
            if i.device_id in synced_devices and i.id not in valid_iface_ids and i.id not in self.created_ids
            and i.if_type == "physical" and i.parent_id is None
            and i.speed_gbps is None and not i.description and not i.ipv4_address and i.mtu is None
        }

    def capacity(self, adj: _Adjacency) -> float | None:
        speeds = []
        for pair in adj.member_pairs:
            ends = [self.ifaces[i].speed_gbps for i in pair]
            if any(s is None for s in ends):
                return None
            speeds.append(min(ends))
        return sum(speeds) if speeds else None


EVENT_RETENTION_DAYS = 90


class _EventLog:
    """Topology events of one discovery run. The very first run (empty link table) is an import, not news:
    it gets one summary event instead of thousands of "link added"."""

    def __init__(self, db: Session, now: datetime, first_run: bool):
        self.db, self.now, self.first_run = db, now, first_run

    def add(self, kind: str, link: Link, detail: str | None = None) -> None:
        self.db.add(TopologyEvent(at=self.now, kind=kind, link_id=link.id, device_id=link.a_device_id,
                                  title=link.name[:255], detail=detail))

    def link_changed(self, link: Link, previous: str | None) -> None:
        if previous is None and not self.first_run:
            self.add("link_added", link, f"{link.capacity_gbps:g} Gbps" if link.capacity_gbps else None)
        elif previous in ("up", "down") and previous != link.status:
            self.add(f"link_{link.status}", link, "port operasional" if link.status == "up" else "port tidak up")

    def finish(self, created: int) -> None:
        if self.first_run and created:
            self.db.add(TopologyEvent(at=self.now, kind="discovery", title=f"Topologi awal: {created} link ditemukan"))
        self.db.execute(delete(TopologyEvent).where(
            TopologyEvent.at < self.now - timedelta(days=EVENT_RETENTION_DAYS)))


def discover_links(db: Session, valid_iface_ids: set[int] | None = None) -> DiscoveryResult:
    """Rebuild links. `valid_iface_ids` (ports confirmed by the NMS this run) enables cleanup of
    phantom ports left by earlier runs; without it nothing is treated as phantom."""
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
    events = _EventLog(db, now, first_run=not existing)

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
        previous = None if link.id is None else link.status
        link.status = "up" if up else "down"
        link.last_seen = now
        if link.discovery_source != "manual":
            link.discovery_source = adj.source
        db.flush()
        seen_ids.add(link.id)
        events.link_changed(link, previous)

    # Discovered links that disappeared from LLDP/config are flagged, not deleted —
    # capacity history and customer references stay intact. Exceptions, which were superseded
    # rather than lost:
    #  - a member-level link whose ports now belong to a discovered bundle;
    #  - a config-described link between devices LLDP now sees directly;
    #  - a link on a phantom port (created from an LLDP name that matched no real port).
    member_pairs = {pair for adj in run.adjacencies.values() if adj.link_type == "bundle" for pair in adj.member_pairs}
    lldp_device_pairs = {
        frozenset((adj.a.device_id, adj.b.device_id)) for adj in run.adjacencies.values() if adj.source == "lldp"
    }
    phantom = run.phantom_ids(valid_iface_ids)
    marked_down = superseded = 0
    for key, link in existing.items():
        if link.id in seen_ids or link.discovery_source not in ("lldp", "config"):
            continue
        replaced = (
            key in member_pairs
            or (link.discovery_source == "config" and frozenset((link.a_device_id, link.b_device_id)) in lldp_device_pairs)
            or bool(key & phantom)
        )
        if replaced:
            db.execute(delete(UtilizationSample).where(UtilizationSample.link_id == link.id))
            db.delete(link)
            superseded += 1
        elif link.status == "up":
            link.status = "down"
            marked_down += 1
            events.add("link_down", link, "tidak terlihat lagi di LLDP/config")
    events.finish(created)
    db.flush()

    removed = 0
    if phantom:
        in_use = {i for l in db.scalars(select(Link)) for i in (l.a_interface_id, l.b_interface_id) if i}
        for iface_id in phantom - in_use:
            db.delete(run.ifaces[iface_id])
            removed += 1

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
        interfaces_removed=removed,
        unresolved_neighbors=unresolved,
    )
