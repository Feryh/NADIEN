"""Topology discovery.

LLDP neighbor tables are the mandatory source. Interface descriptions taken from the
device configuration are the backup, used only for ports LLDP did not cover
(e.g. LLDP disabled on a legacy box or a neighbor across a transparent L2 path).

When both ends of an LLDP adjacency are bundle members, the link is recorded once
at bundle level with capacity = sum of member speeds.
"""

import ipaddress
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from ..models import Device, Interface, Link, LinkApproval, LldpNeighbor, TopologyEvent, UtilizationSample
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
_SLOT_PORT = re.compile(r"(\d+):(\d+)")  # FiberHome port id in neighbours' LLDP (9:1 = SFP+ 9/1)
_MAC = re.compile(r"[0-9a-fA-F]{12}")
_HOSTNAME_SHAPED = re.compile(r"(?<![A-Za-z0-9])([A-Za-z][A-Za-z0-9]*)(?:[-_][A-Za-z0-9]+){2,}")


def host_key(name: str) -> str:
    """'JKT_P_01.metro.net' -> 'jkt-p-01': the form used to spot hostnames inside descriptions."""
    return "-".join(p for p in _SPLIT.split(norm_host(name)) if p)


def _city(name: str) -> str | None:
    """'PAD-PAU-BNG1-M4' -> 'pad', cell site 'SUL-SA-MND-0230-ME1-ZTE' -> 'mnd'. None for role-first names
    ('PEIN1-H-SIT-PNJ'), whose first part is no city."""
    csr = csr_parts(name.replace("_", "-"))
    if csr:
        return csr[2].lower()
    first = host_key(name).split("-")[0]
    return first if first.isalpha() else None


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
        # A decommissioned box (P1-NCS5504-BDL-KDT, SRG-SMY-ME1-FH820) keeps its last LLDP table, config and
        # addresses; none of them may draw a link any more.
        self.devices = {d.id: d for d in db.scalars(select(Device).where(Device.status != "decommissioned"))}
        self.by_host = {norm_host(d.hostname): d for d in self.devices.values()}
        self.by_host_key = {host_key(d.hostname): d for d in self.devices.values()}
        self._add_site_keys()
        self.host_first_parts = {k.split("-")[0] for k in self.by_host_key}
        self.max_host_parts = max((k.count("-") + 1 for k in self.by_host_key), default=1)
        self.ifaces = {i.id: i for i in db.scalars(select(Interface)) if i.device_id in self.devices}
        self.if_index = {(i.device_id, norm_if(i.name)): i for i in self.ifaces.values()}
        self.adjacencies: dict[frozenset[int], _Adjacency] = {}
        self.covered: set[int] = set()
        # Port pairs only one end describes while the other end's description names a third device.
        self.contradicted: set[frozenset[int]] = set()
        # LLDP is the authority on what is plugged into a port. Ports (and the bundle of such a member) whose LLDP
        # neighbour names a system the inventory lacks (a carrier/transport box): no description may pair them.
        self.lldp_seen: set[int] = set()
        # Ports whose LLDP neighbour sends only its MAC (cell-site ZTE boxes do): -> the neighbour's port names. A
        # description may pair such a port only with a device that has a port of that name.
        self.lldp_mac_ports: dict[int, set[str]] = {}
        # Ports a description named a peer for, refused because their LLDP neighbour cannot be that peer.
        self.lldp_refused: set[int] = set()
        self.created_ids: set[int] = set()
        from ..config import settings  # read at run time, like fresh_lldp: tests change it

        # A port down longer than LLDP rows may age: what the NMS still lists on it, or its description says, is
        # history (SRG-NDC-SW1-NX93: six OLTs on ports down since August 2025, the OLTs long moved elsewhere).
        self.dead_before = (datetime.now(timezone.utc) - timedelta(days=settings.lldp_max_age_days)
                            if settings.lldp_max_age_days > 0 else None)
        self._ports_by_device: dict[int, list[Interface]] | None = None
        self._by_stem: dict[str, list[Device]] | None = None

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
        if found is None or found.speed_gbps is None:
            # The bare `slot:port` name is only a stand-in when no real port of that name exists (an earlier run
            # created it from LLDP): the twin the NMS reports wins.
            found = self.slot_port_twin(device, name) or found
        if found is None:
            found = Interface(device_id=device.id, name=name.strip(), if_type="physical", oper_status="up")
            self.db.add(found)
            self.db.flush()
            self.if_index[key] = found
            self.ifaces[found.id] = found
            self.created_ids.add(found.id)
            self.stats["interfaces_created"] += 1
        return found

    def slot_port_twin(self, device: Device, name: str) -> Interface | None:
        """FiberHome ports reach neighbours' LLDP as `slot:port` (9:1) while the NMS names them `SFP+ 9/1`: the one
        port of the device ending in that slot/port, so both LLDP directions land on the same port."""
        match = _SLOT_PORT.fullmatch(name.strip())
        if not match:
            return None
        tail = re.compile(rf"(?:^|[\s_-]){match.group(1)}/{match.group(2)}$")
        twins = [i for i in self.ports_of(device.id) if i.name != name.strip() and tail.search(i.name.strip())]
        return twins[0] if len(twins) == 1 else None

    def ports_of(self, device_id: int) -> list[Interface]:
        if self._ports_by_device is None:
            self._ports_by_device = {}
            for iface in self.ifaces.values():
                if iface.if_type not in ("subinterface", "virtual"):  # a link ends on a port or a bundle
                    self._ports_by_device.setdefault(iface.device_id, []).append(iface)
        return self._ports_by_device.get(device_id, [])

    def add(self, a: Interface, b: Interface, source: str) -> None:
        """Every source ends here, so a port down past the LLDP age limit links nothing, whichever source names it:
        LLDP the NMS keeps, a description, a /30 still configured, an approval (kept: it applies again once the port
        is back)."""
        member_pair = frozenset((a.id, b.id))
        if a.parent_id and b.parent_id:
            a, b, link_type = self.ifaces[a.parent_id], self.ifaces[b.parent_id], "bundle"
        else:
            link_type = "metro_e"
        if any(self.long_down(i) for i in (a, b, *(self.ifaces[j] for j in member_pair if j in self.ifaces))):
            self.stats[f"{source}_long_down"] += 1
            return
        key = frozenset((a.id, b.id))
        adj = self.adjacencies.setdefault(key, _Adjacency(a, b, link_type, source))
        adj.member_pairs.add(member_pair)
        self.covered.update(member_pair)
        self.covered.update(key)

    def long_down(self, iface: Interface) -> bool:
        """Down since before the LLDP age limit. An unknown change time (not synced yet) is not old: a port that went
        down in an outage keeps its link, drawn down."""
        changed = iface.oper_changed_at
        if changed is None and iface.parent_id in self.ifaces:
            # A member the NMS does not report (made up from the far end's LLDP, so "up" by default) is as dead as
            # its bundle: SRG-SRG-OLT1-ZTE still names NX93 Ethernet1/5-6, whose port-channel14 is down since 2025.
            return self.long_down(self.ifaces[iface.parent_id])
        if self.dead_before is None or iface.oper_status == "up" or changed is None:
            return False
        if changed.tzinfo is None:  # SQLite hands back naive UTC
            changed = changed.replace(tzinfo=timezone.utc)
        return changed < self.dead_before

    def from_lldp(self) -> list[str]:
        unresolved = []
        for n in self.db.scalars(fresh_lldp(self.db)):
            local = self.devices.get(n.device_id)
            remote = self.by_host.get(norm_host(n.remote_system_name))
            if local is None or remote is None:
                unresolved.append(f"{local.hostname if local else n.device_id}:{n.local_interface} -> {n.remote_system_name}")
                port = self.if_index.get((local.id, norm_if(n.local_interface))) if local else None
                if port is None:
                    continue
                for port_id in (i for i in (port.id, port.parent_id) if i):
                    if _MAC.fullmatch(re.sub(r"[:.\-\s]", "", n.remote_system_name or "")):
                        self.lldp_mac_ports.setdefault(port_id, set()).add(norm_if(n.remote_port_id or ""))
                    else:
                        self.lldp_seen.add(port_id)
                continue
            if self.is_logical(local, n.local_interface) or self.is_logical(remote, n.remote_port_id):
                # A VLAN interface, management port or the like (Vbdif59, MEth0/0/1, spi-0/3/0/1) carries no link of
                # its own: the neighbour announced it instead of the physical port. The pair stays open for the
                # config, whose description may name the real port.
                self.stats["lldp_logical_ports"] += 1
                continue
            self.add(self.iface(local, n.local_interface), self.iface(remote, n.remote_port_id), "lldp")
        self.drop_minority_bundles()
        return unresolved

    def drop_minority_bundles(self) -> None:
        """A bundle has one LACP partner: towards one device it pairs with one bundle. When its members' LLDP points
        to two bundles of the same box, the one most members agree on wins — the NMS sometimes files a neighbour
        under the wrong local port (a ZTE M6K's cgei-0/1/0/2 neighbour shown on cgei-0/2/1/8, a member of a retired
        smartgroup99), and the stray pairing would read the whole bundle's traffic on one 100G member's capacity."""
        rivals: dict[tuple[int, int], list[frozenset[int]]] = {}
        for key, adj in self.adjacencies.items():
            if adj.link_type == "bundle" and adj.source == "lldp":
                for mine, other in ((adj.a, adj.b), (adj.b, adj.a)):
                    rivals.setdefault((mine.id, other.device_id), []).append(key)
        for keys in rivals.values():
            live = [k for k in keys if k in self.adjacencies]
            if len(live) < 2:
                continue
            votes = sorted(live, key=lambda k: -len(self.adjacencies[k].member_pairs))
            if len(self.adjacencies[votes[0]].member_pairs) == len(self.adjacencies[votes[1]].member_pairs):
                continue  # no majority: keep both rather than guess
            for key in votes[1:]:
                del self.adjacencies[key]
                self.stats["lldp_minority_bundles"] += 1

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
        "TO-X" would be paired on top of its LLDP-discovered members and counted twice. Never where LLDP rules the
        described peer out (lldp_allows): the description then names the far end of a carrier or transport path
        (KDR-PUW-BNG1-M4 Eth-Trunk1 "to MKS-BSW-BNG2-M4" over INDOSAT), not what is plugged into the port.
        Ports are grouped by device pair first, so the work is linear in the number of ports.
        """
        lldp_pairs = {frozenset((adj.a.device_id, adj.b.device_id)) for adj in self.adjacencies.values()}
        by_pair: dict[frozenset[int], dict[int, list[Interface]]] = {}
        for iface in self.ifaces.values():
            if (iface.id in self.covered or iface.id in self.lldp_seen or iface.parent_id or not iface.description
                    or iface.if_type in ("subinterface", "virtual") or self.long_down(iface)):
                continue
            remote = self.mentioned_device(iface.description)
            if remote is None or remote.id == iface.device_id or not self.lldp_allows(iface, remote.id):
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
            for a, b in self.best_config_pairs(ours, theirs):
                self.add(a, b, "config")

    @staticmethod
    def best_config_pairs(ours: list[Interface], theirs: list[Interface]) -> list[tuple[Interface, Interface]]:
        """Ports of two devices that describe each other, paired best match first, each port used once.

        Descriptions alone rarely say which port is which; a stale one survives on a port taken out of service
        (BKS-LBJ-ME1-M8 GigabitEthernet0/8/0, 10G down, still "to BKS-NBK-ME1-6120HS"). Matching in port order paired
        the live 200G smartgroup4 with it. Ranked instead: a description naming the other port, then bundle with
        bundle, both up, then the same speed; ties keep port order."""
        def score(a: Interface, b: Interface) -> tuple[bool, bool, bool, bool]:
            named = (_names_port(norm_if(b.description or ""), norm_if(a.name))
                     or _names_port(norm_if(a.description or ""), norm_if(b.name)))
            same_kind = (a.if_type == "bundle") == (b.if_type == "bundle")
            both_up = a.oper_status == b.oper_status == "up"
            same_speed = bool(a.speed_gbps) and a.speed_gbps == b.speed_gbps
            return named, same_kind, both_up, same_speed

        candidates = sorted(((a, b) for a in ours for b in theirs), key=lambda pair: score(*pair), reverse=True)
        used: set[int] = set()
        pairs = []
        for a, b in candidates:
            if a.id not in used and b.id not in used:
                used.update((a.id, b.id))
                pairs.append((a, b))
        return pairs

    def from_p2p(self) -> None:
        """Two routers on the same /30 or /31 are linked, whatever box (a carrier's NNI, a transport node) LLDP sees
        in between. The link goes on the port carrying the address (100GE1/1/1 for 100GE1/1/1.536). LLDP still
        decides: a port LLDP links to an inventory device is left alone. Runs after the descriptions and only fills
        a gap: a /30 often crosses an L2 switch (PEIN Eth-Trunk30 -> DS1 switch -> PE1), and the description names
        the switch, the cable's real far end. So at least one end must have no link yet; the other may be shared, as
        a carrier NNI port carries one /30 per far end."""
        lldp_ports: set[int] = set()
        for adj in self.adjacencies.values():
            if adj.source == "lldp":
                lldp_ports.update((adj.a.id, adj.b.id))
                for pair in adj.member_pairs:
                    lldp_ports.update(pair)
        lldp_ports = _with_parents(self, frozenset(lldp_ports))  # LLDP on a member speaks for its bundle
        # A pair an engineer approved by hand is their call: an address pair does not take its port away.
        for approval in self.db.scalars(select(LinkApproval)):
            lldp_ports.update((approval.a_interface_id, approval.b_interface_id))
        index = _P2pIndex(self)
        for holders in index.by_net.values():
            ends = _live_holders(holders)
            if len(ends) != 2 or ends[0].device_id == ends[1].device_id:
                continue
            a, b = (self._link_port(index.carrier(end)) for end in ends)
            if (a.if_type not in ("physical", "bundle") or b.if_type not in ("physical", "bundle")
                    or a.id in lldp_ports or b.id in lldp_ports or frozenset((a.id, b.id)) in self.adjacencies
                    or (a.id in self.covered and b.id in self.covered)):
                continue
            self.add(a, b, "ip")

    def _link_port(self, iface: Interface) -> Interface:
        """A bundle member carrying an address links through its bundle."""
        return self.ifaces.get(iface.parent_id, iface) if iface.parent_id else iface

    def from_approvals(self) -> None:
        """Pairs an engineer confirmed by hand (LinkApproval), unless LLDP or the config already linked either port:
        once the box in between is in the inventory, what LLDP sees wins and the approval is no longer used.
        Approved pairs may share a port: a hub's trunk often reaches several sites over a carrier, one sub-interface
        each (MKS-BSW-BNG2-M8 Eth-Trunk40.100 to KDR, .400 to GTO)."""
        taken = set(self.covered)
        for approval in self.db.scalars(select(LinkApproval)):
            a, b = self.ifaces.get(approval.a_interface_id), self.ifaces.get(approval.b_interface_id)
            if a is None or b is None:
                continue
            # A shared approval rides a port that also carries another link (third-party NNI, one VLAN per far end).
            if approval.shared or (a.id not in taken and b.id not in taken):
                self.add(a, b, "approved")

    def one_sided(self, pair: frozenset[int], sides: dict[int, list[Interface]]) -> None:
        """Only one end describes the other (typical for a cell-site uplink to a metro box that is
        not described back). Accepted only when the description also names an existing, uncovered
        port of the remote device — the hostname alone is not enough to call it a link — and that port's own
        description does not name a third device: then ours is a stale or mistyped description
        (PAD-PAU-BNG1-M4 Eth-Trunk74 "to KDR-PUW-BNG1-M4:Eth-Trunk1" while that trunk says "to MKS-BSW-BNG2-M4")."""
        ((device_id, ours),) = sides.items()
        remote_id = next(iter(pair - {device_id}))
        for a in ours:
            b = self.named_port(remote_id, a.description or "")
            if b is None or b.id in self.covered or b.id in self.lldp_seen or not self.lldp_allows(b, device_id):
                continue
            if self.names_another_device(b, device_id):
                self.contradicted.add(frozenset((a.id, b.id)))
                continue
            self.add(a, b, "config")

    def is_logical(self, device: Device, port_name: str | None) -> bool:
        """A port that is no physical port or bundle: classified virtual from the NMS, or named like one."""
        from ..collectors.observium.parsers import VIRTUAL_NAME  # late: the parsers import this module

        port = self.if_index.get((device.id, norm_if(port_name or "")))
        if port is not None:
            return port.if_type == "virtual"
        return bool(VIRTUAL_NAME.match((port_name or "").strip()))

    def replacement_for(self, hostname: str) -> Device | None:
        """The one inventory device a description most likely means by a name the inventory does not have: the name
        with another model suffix (MKS-BSW-BNG2-M4 -> MKS-BSW-BNG2-M8 after a hardware swap), else the name written
        without its suffix (JKT-CSF-SPE1 -> JKT-CSF-SPE1-ASR9K). None when no or several devices qualify."""
        key = host_key(hostname)
        parts = key.split("-")
        if len(parts) < 3:
            return None
        if self._by_stem is None:
            self._by_stem = {}
            for device in self.devices.values():
                stem = "-".join(host_key(device.hostname).split("-")[:-1])
                self._by_stem.setdefault(stem, []).append(device)
        found = self._by_stem.get("-".join(parts[:-1]), [])
        if not found:
            found = self._by_stem.get(key, [])  # the devices named `hostname` plus one more part
        return found[0] if len(found) == 1 else None

    def lldp_allows(self, port: Interface, peer_device_id: int) -> bool:
        """LLDP does not rule out `peer_device_id` behind `port`: the neighbour is no unknown named system, and a
        neighbour that sent only its MAC reported port names `peer_device_id` has (a cell site's xgei-1/1/0/9 — yes;
        a carrier ZTE's xxvgei-1/1/0/12 behind a Huawei peer — no)."""
        seen = self.lldp_mac_ports.get(port.id)
        if seen is None:
            return True
        peer_ports = {norm_if(p.name) for p in self.ports_of(peer_device_id)}
        if seen <= peer_ports:
            return True
        self.lldp_refused.add(port.id)
        return False

    def names_another_device(self, port: Interface, expected_device_id: int) -> bool:
        """The port's description points to a city other than `expected_device_id`'s and other than its own:
        hostname-shaped words (three or more -/_ joined parts starting like an inventory hostname, known to the NMS
        or not — MKS-BSW-BNG2-M4 is not) compared by their city code. Descriptions often spell the same box
        differently (DUM-DMB for DUM-DUB) or keep the name of a box that was replaced in the same city (BJM-NDC
        for BJM-STO), and role-first names (PEIN1-H-…) carry no city, so only a different city is a contradiction."""
        expected = _city(self.devices[expected_device_id].hostname)
        if expected is None:
            return False
        cities = {_city(m.group(0)) for m in _HOSTNAME_SHAPED.finditer(port.description or "")
                  if m.group(1).lower() in self.host_first_parts}
        cities.discard(_city(self.devices[port.device_id].hostname))  # a local OLT/box, not a remote peer
        return bool(cities) and None not in cities and expected not in cities

    def named_port(self, device_id: int, description: str) -> Interface | None:
        """Port of `device_id` written in the description, longest name wins (xgei-1/1/0/10 over /1)."""
        text = norm_if(description)
        best: Interface | None = None
        for iface in self.ports_of(device_id):
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
            # A far port the NMS does not report (a box only known from config or LLDP) has no speed: the end that
            # does report one bounds the pair, rather than a 0G link reading its traffic as an overload.
            ends = [s for s in (self.ifaces[i].speed_gbps for i in pair) if s]
            if not ends:
                speeds = []
                break
            speeds.append(min(ends))
        members = sum(speeds) if speeds else None
        if adj.link_type != "bundle":
            return members
        # A bundle's own SNMP speed counts every active member; LLDP or the config often show only some of them
        # (port-channel30 = 40G with one of its four members known). The smaller end bounds the bundle.
        bundle_ends = [adj.a.speed_gbps, adj.b.speed_gbps]
        bundle = min(bundle_ends) if all(s and s > 0 for s in bundle_ends) else None
        return max(v for v in (members, bundle) if v is not None) if members or bundle else None


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


def _with_parents(run: _Discovery, port_ids: frozenset[int]) -> set[int]:
    ports = set(port_ids)
    ports.update(run.ifaces[i].parent_id for i in port_ids if i in run.ifaces and run.ifaces[i].parent_id)
    return ports


def discover_links(db: Session, valid_iface_ids: set[int] | None = None) -> DiscoveryResult:
    """Rebuild links. `valid_iface_ids` (ports confirmed by the NMS this run) enables cleanup of
    phantom ports left by earlier runs; without it nothing is treated as phantom."""
    run = _Discovery(db)
    unresolved = run.from_lldp()
    run.from_config()
    run.from_p2p()  # after the descriptions: a /30 may cross an L2 switch the description names as the far end
    run.from_approvals()

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
        up = all(run.ifaces[i].oper_status == "up" for i in key) if adj.source != "lldp" else True
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
    #  - a link on a phantom port (created from an LLDP name that matched no real port);
    #  - a config-described link the far end's own description now contradicts (it never existed);
    #  - a config-described link on a port where LLDP sees a box outside the inventory (LLDP decides);
    #  - a link on a port down for longer than LLDP rows may age (lldp_max_age_days).
    member_pairs = {pair for adj in run.adjacencies.values() if adj.link_type == "bundle" for pair in adj.member_pairs}
    lldp_device_pairs = {
        frozenset((adj.a.device_id, adj.b.device_id)) for adj in run.adjacencies.values() if adj.source == "lldp"
    }
    phantom = run.phantom_ids(valid_iface_ids)
    marked_down = superseded = 0
    for key, link in existing.items():
        if link.id in seen_ids or link.discovery_source not in ("lldp", "config", "approved", "ip"):
            continue
        replaced = (
            key in member_pairs
            or (link.discovery_source == "config" and frozenset((link.a_device_id, link.b_device_id)) in lldp_device_pairs)
            or bool(key & phantom)
            or (link.discovery_source == "config"
                and (key in run.contradicted or bool(key & (run.lldp_seen | run.lldp_refused))))
            # a config-described link whose port, or that port's bundle, a current link now uses: an earlier
            # pairing of the same descriptions, not a second link (else every re-pairing leaves one down behind)
            or (link.discovery_source == "config" and bool(_with_parents(run, key) & run.covered))
            or link.discovery_source == "approved"  # approval revoked, or LLDP now sees the ports directly
            or link.discovery_source == "ip"  # the address pair is gone, or LLDP now links one of its ports
            or any(run.ifaces[i].if_type == "virtual" for i in key if i in run.ifaces)  # a VLAN/management interface
            # a port of it down past the LLDP age limit: the cable is gone, not a link in an outage
            or any(run.long_down(run.ifaces[i]) for i in key if i in run.ifaces)
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
        ip_links=sources["ip"],
        created=created,
        updated=updated,
        marked_down=marked_down,
        superseded=superseded,
        interfaces_created=run.stats["interfaces_created"],
        interfaces_removed=removed,
        unresolved_neighbors=unresolved,
    )


def fresh_lldp(db: Session):
    """LLDP rows refreshed within `lldp_max_age_days` of the newest ones. A sync replaces only the tables of the
    devices it polls, so a box Observium dropped (SRG-SMY-ME1-FH820, swapped for SRG-SMY-ME1-M8) keeps its last
    table; measured against the newest rows, a sync that has not run for a while does not age everything out."""
    from ..config import settings

    newest = db.scalar(select(func.max(LldpNeighbor.collected_at)))
    stmt = select(LldpNeighbor)
    if newest is None or settings.lldp_max_age_days <= 0:
        return stmt
    return stmt.where(LldpNeighbor.collected_at >= newest - timedelta(days=settings.lldp_max_age_days))


def _p2p_net(address: str | None) -> ipaddress.IPv4Network | None:
    """The /30 or /31 an address sits in: such a subnet has room for two routers only."""
    try:
        network = ipaddress.ip_interface((address or "").strip()).network
    except ValueError:
        return None
    return network if network.version == 4 and network.prefixlen in (30, 31) else None


def _live_holders(holders: list[Interface]) -> list[Interface]:
    """The interfaces that really hold a point-to-point address. The same /30 is often left configured on a second
    box (a standby or retired BNG: DPS-NDC-BNG2 Eth-Trunk20.948 next to DPS-NDC-ME1 Eth-Trunk4.948); with more than
    two holders only those up count."""
    if len(holders) <= 2:
        return holders
    return [i for i in holders if i.oper_status == "up"]


class _P2pIndex:
    """Interfaces per point-to-point subnet. Two routers on the same /30 or /31 are linked, whatever box (a carrier's
    NNI, a transport node) LLDP sees in between. Sub-interfaces carry most of these addresses (100GE1/1/1.536); their
    link is drawn on the port that carries them, found by name since the inventory gives them no parent."""

    def __init__(self, run: "_Discovery"):
        self.run = run
        self.by_net: dict[ipaddress.IPv4Network, list[Interface]] = defaultdict(list)
        self.subs: dict[int, list[Interface]] = defaultdict(list)  # carrying port id -> its sub-interfaces
        for iface in run.ifaces.values():
            network = _p2p_net(iface.ipv4_address)
            if network is not None:
                self.by_net[network].append(iface)
            carrier = self.carrier(iface)
            if carrier is not iface:
                self.subs[carrier.id].append(iface)

    def carrier(self, iface: Interface) -> Interface:
        if "." not in iface.name:
            return iface
        return self.run.if_index.get((iface.device_id, norm_if(iface.name.rsplit(".", 1)[0]))) or iface

    def peer(self, port: Interface) -> dict[str, Any] | None:
        """The far end sharing a /30 or /31 with the port or one of its sub-interfaces; None unless exactly one."""
        for own in (port, *sorted(self.subs.get(port.id, []), key=lambda i: i.name)):
            network = _p2p_net(own.ipv4_address)
            if network is None:
                continue
            others = [i for i in _live_holders(self.by_net[network]) if i.device_id != port.device_id]
            if len(others) == 1:
                far = others[0]
                return {"subnet": str(network), "local_port": own.name, "local_ip": own.ipv4_address,
                        "remote_port": far.name, "remote_ip": far.ipv4_address, "peer": self.carrier(far)}
        return None


def unknown_lldp_peers(db: Session) -> list[dict[str, Any]]:
    """Ports whose description (or their bundle's) names a peer that LLDP rules out: the neighbour is a system
    outside the inventory (a transport node), or sent only its MAC and a port name the described peer does not have
    (a carrier's box). One row per port that would carry the link (the bundle, else the port), with the peer port the
    description names and any approval (LinkApproval) an engineer gave it."""
    run = _Discovery(db)
    approvals: dict[int, LinkApproval] = {}
    for approval in db.scalars(select(LinkApproval)):
        approvals[approval.a_interface_id] = approvals[approval.b_interface_id] = approval
    # Ports LLDP or the config already link: approving onto one is refused (approve_link), so say so up front.
    taken: dict[int, str] = {}
    # Approved and address-pair links may share a port (a carrier NNI), so they block nothing.
    for link in db.scalars(select(Link).where(Link.status == "up", Link.discovery_source.not_in(("approved", "ip")))):
        for port_id in (link.a_interface_id, link.b_interface_id):
            if port_id:
                taken[port_id] = link.name
    rows: dict[int, dict[str, Any]] = {}
    p2p = _P2pIndex(run)
    drawn = {frozenset((link.a_interface_id, link.b_interface_id))
             for link in db.scalars(select(Link).where(Link.discovery_source == "ip"))}
    for n in db.scalars(fresh_lldp(db)):
        local = run.devices.get(n.device_id)
        if local is None or run.by_host.get(norm_host(n.remote_system_name)) is not None:
            continue
        port = run.if_index.get((local.id, norm_if(n.local_interface)))
        if port is None:
            continue
        bundle = run.ifaces.get(port.parent_id) if port.parent_id else None
        target = bundle or port
        described = next((p for p in (target, port) if p.description), None)
        text = described.description if described else ""
        named = run.mentioned_device(text) if text else None
        # The same /30 or /31 on both routers settles the far end, even when the description names none or an old box.
        ip_match = p2p.peer(target) or (p2p.peer(port) if port is not target else None)
        if named is not None and named.id == local.id:
            if ip_match is None:
                continue  # the description names this box itself: nothing to pair
            named = None
        token = next((m.group(0) for m in _HOSTNAME_SHAPED.finditer(text)
                      if m.group(1).lower() in run.host_first_parts
                      and _city(m.group(0)) != _city(local.hostname)), None)
        if named is None and token is None and ip_match is None:
            continue
        if ip_match is not None and frozenset((target.id, ip_match["peer"].id)) in drawn:
            continue  # discovery already draws this pair from the shared /30 or /31
        mac_only = bool(_MAC.fullmatch(re.sub(r"[:.\-\s]", "", n.remote_system_name or "")))
        if mac_only and named is not None and norm_if(n.remote_port_id or "") in {
                norm_if(p.name) for p in run.ports_of(named.id)}:
            continue  # the described peer itself, announcing only its MAC (cell sites do)
        row = rows.get(target.id)
        if row is None:
            suggested = run.replacement_for(token) if named is None and token else None
            candidate = named or suggested
            peer = run.named_port(candidate.id, text) if candidate else None
            if ip_match is not None:
                peer = ip_match["peer"]
            approval = approvals.get(target.id)
            if approval is not None:  # the pair an approver chose, which may differ from the description
                peer = run.ifaces.get(approval.b_interface_id if approval.a_interface_id == target.id
                                      else approval.a_interface_id)
            peer_device = run.devices.get(peer.device_id) if peer else candidate
            row = rows[target.id] = {
                "device_id": local.id, "device": local.hostname, "interface_id": target.id, "port": target.name,
                "is_bundle": bundle is not None, "lldp": [], "description": text,
                "described_device_id": named.id if named else None,
                "described_device": named.hostname if named else token or "",
                # Both ends on one /30 or /31: the far end found by address, not by description.
                "ip_match": None if ip_match is None else {
                    k: v for k, v in ip_match.items() if k != "peer"} | {"peer_port": ip_match["peer"].name},
                # A box the description names under an old name (MKS-BSW-BNG2-M4 for its replacement -M8).
                "suggested_device": suggested.hostname if suggested and not named else None,
                "peer_device_id": peer_device.id if peer_device else None,
                "peer_device": peer_device.hostname if peer_device else None,
                "peer_interface_id": peer.id if peer else None, "peer_port": peer.name if peer else None,
                # The link already on the peer port (or on this one), when that blocks approving the pair.
                "taken_by": None if approval is not None else taken.get(target.id) or (taken.get(peer.id) if peer else None),
                "approval": None if approval is None else {
                    "id": approval.id, "approved_by": approval.approved_by, "approved_at": approval.approved_at,
                    "shared": approval.shared},
            }
        row["lldp"].append({"local_port": port.name, "neighbor": n.remote_system_name, "port": n.remote_port_id})
    return sorted(rows.values(), key=lambda r: (r["device"], r["port"]))


class ApprovalError(ValueError):
    """A pair that cannot be approved; `status` is the HTTP status to answer with."""

    def __init__(self, message: str, status: int):
        super().__init__(message)
        self.status = status


def _link_on(db: Session, a_id: int, b_id: int) -> Link | None:
    return db.scalar(select(Link).where(
        ((Link.a_interface_id == a_id) & (Link.b_interface_id == b_id))
        | ((Link.a_interface_id == b_id) & (Link.b_interface_id == a_id))))


def approve_link(db: Session, interface_id: int, peer_interface_id: int, username: str, shared: bool = False) -> int:
    """Record the approval and draw the link now (discovery keeps it from then on). Returns the link id.

    `shared`: the port carries another link on purpose (a bundle to a third-party NNI, one VLAN per far end), so a
    link LLDP or the config already has on it does not block the approval."""
    a, b = db.get(Interface, interface_id), db.get(Interface, peer_interface_id)
    if a is None or b is None:
        raise ApprovalError(f"Port {interface_id if a is None else peer_interface_id} tidak ditemukan", 404)
    if a.device_id == b.device_id:
        raise ApprovalError("Kedua port ada di perangkat yang sama", 422)
    link = _link_on(db, a.id, b.id)
    # Approved links may share a port (a hub trunk to several sites); a port LLDP or the config links is taken.
    busy = db.scalar(select(Link).where(Link.status == "up", Link.discovery_source.not_in(("approved", "ip")),
                                        Link.id != (link.id if link else -1),
                                        Link.a_interface_id.in_((a.id, b.id)) | Link.b_interface_id.in_((a.id, b.id))))
    if busy is not None and not shared:
        raise ApprovalError(f"Port sudah dipakai link lain: {busy.name}. Bila port ini memang dipakai bersama "
                            "(NNI pihak ketiga, beda VLAN), setujui sebagai port bersama.", 409)
    other = db.scalar(select(LinkApproval).where(
        (LinkApproval.a_interface_id == a.id) | (LinkApproval.b_interface_id == a.id))
        .where(~((LinkApproval.a_interface_id == min(a.id, b.id)) & (LinkApproval.b_interface_id == max(a.id, b.id)))))
    if other is not None:
        raise ApprovalError("Port ini sudah disetujui ke port lain; cabut persetujuan itu dulu", 409)
    low, high = sorted((a.id, b.id))
    approval = db.scalar(select(LinkApproval).where(LinkApproval.a_interface_id == low,
                                                    LinkApproval.b_interface_id == high))
    if approval is None:
        db.add(LinkApproval(a_interface_id=low, b_interface_id=high, approved_by=username[:64], shared=shared))
    elif shared:
        approval.shared = True
    if link is None:
        a_dev, b_dev = db.get(Device, a.device_id), db.get(Device, b.device_id)
        speeds = [s for s in (a.speed_gbps, b.speed_gbps) if s]
        link = Link(name=f"{a_dev.hostname}:{a.name} <> {b_dev.hostname}:{b.name}", a_device_id=a.device_id,
                    a_interface_id=a.id, b_device_id=b.device_id, b_interface_id=b.id,
                    capacity_gbps=min(speeds) if speeds else 0,
                    link_type="bundle" if a.if_type == b.if_type == "bundle" else "metro_e")
        db.add(link)
    if link.discovery_source != "manual":
        link.discovery_source = "approved"
    link.status = "up" if a.oper_status == b.oper_status == "up" else "down"
    link.last_seen = datetime.now(timezone.utc)
    db.flush()
    db.add(TopologyEvent(at=datetime.now(timezone.utc), kind="link_added", link_id=link.id, device_id=link.a_device_id,
                         title=link.name[:255], detail=f"disetujui manual oleh {username}"))
    return link.id


def revoke_link(db: Session, approval_id: int) -> bool:
    """Drop the approval and the link drawn only because of it. False when there is no such approval."""
    approval = db.get(LinkApproval, approval_id)
    if approval is None:
        return False
    link = _link_on(db, approval.a_interface_id, approval.b_interface_id)
    if link is not None and link.discovery_source == "approved":
        db.execute(delete(UtilizationSample).where(UtilizationSample.link_id == link.id))
        db.delete(link)
    db.delete(approval)
    return True
