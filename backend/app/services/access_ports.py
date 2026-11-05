"""Ports of a PE an L2VPN can be attached to: UP ports facing down towards OLT / access, bundles first.

Where a port leads comes from, in order: a discovered link on that port or bundle, the LLDP neighbour of the
port (a bundle takes its members' neighbour when they all name the same device), else a hint in the port
description written in the device config (OLT, GPON, ACCESS …). Down is any neighbour further from the core
than this device (`ROLE_TIER`): a PE's ports to metro/aggregation or OLT, a metro box's ports to OLT/CSR.

Bundle members are never offered on their own: the service goes on the bundle, so a member port would cut it
out of the LAG. Management, loopback and other virtual interfaces are not access ports either.
"""

import re
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Literal

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from .. import models as m
from .areas import ROLE_TIER
from .segments import UNKNOWN_TIER
from .topology import norm_host, norm_if

Direction = Literal["downlink", "uplink", "peer", "unknown"]

_NOT_ACCESS = re.compile(
    r"^(loopback|inloopback|lo\d|null|vlanif|vlan|meth|mgmt|management|tunnel|nve|virtual|dialer|bdif|vbdif|"
    r"ve\d|irb|bvi|ptp|console|aux)", re.I)
# [ACC] is this network's description tag for access-facing ports (INTERNAL:[ACC]TO-S5700-…).
_DOWNLINK_WORDS = re.compile(r"\b(olt|ont|gpon|epon|xgpon|pon|acc|access|akses|cpe|csr|downlink|dl)\b", re.I)
_UPLINK_WORDS = re.compile(r"\b(uplink|ul|core|backbone|to[-_ ]?p\d|to[-_ ]?pe)\b", re.I)


@dataclass
class Far:
    hostname: str
    role: str | None
    source: Literal["link", "lldp", "description"]


@dataclass
class Candidate:
    name: str
    kind: Literal["bundle", "physical"]
    speed_gbps: float | None
    description: str | None
    members: list[str] = field(default_factory=list)
    far: Far | None = None
    direction: Direction = "unknown"


def _direction(local_role: str | None, far: Far | None, description: str | None) -> Direction:
    if far is not None and far.role is not None:
        local, remote = ROLE_TIER.get(local_role or "", UNKNOWN_TIER), ROLE_TIER.get(far.role, UNKNOWN_TIER)
        if remote == UNKNOWN_TIER:
            return "unknown"
        return "downlink" if remote > local else "uplink" if remote < local else "peer"
    text = " ".join(filter(None, [description, far.hostname if far else None]))
    if _DOWNLINK_WORDS.search(text):
        return "downlink"
    if _UPLINK_WORDS.search(text):
        return "uplink"
    return "unknown"


def access_ports(db: Session, device_id: int) -> dict:
    """UP access-port candidates of a device, downlink bundles first; uplinks and core peers left out."""
    device = db.get(m.Device, device_id)
    if device is None:
        raise LookupError(f"Device {device_id} not found")
    interfaces = list(db.scalars(select(m.Interface).where(m.Interface.device_id == device_id)))
    devices = {d.id: d for d in db.scalars(select(m.Device))}
    by_host = {norm_host(d.hostname): d for d in devices.values()}

    def far_device(other: m.Device | None, source: Literal["link", "lldp"]) -> Far | None:
        return Far(other.hostname, other.role, source) if other else None

    # Discovered links: the far device of each port or bundle that carries one.
    by_iface: dict[int, Far] = {}
    for link in db.scalars(select(m.Link).where(or_(m.Link.a_device_id == device_id, m.Link.b_device_id == device_id))):
        if link.a_device_id == device_id and link.a_interface_id:
            by_iface[link.a_interface_id] = far_device(devices.get(link.b_device_id), "link")
        if link.b_device_id == device_id and link.b_interface_id:
            by_iface[link.b_interface_id] = far_device(devices.get(link.a_device_id), "link")

    # LLDP: far end per local port name (the remote system may be outside the inventory).
    lldp: dict[str, Far] = {}
    for row in db.scalars(select(m.LldpNeighbor).where(m.LldpNeighbor.device_id == device_id)):
        known = by_host.get(norm_host(row.remote_system_name))
        lldp[norm_if(row.local_interface)] = (far_device(known, "lldp") if known
                                              else Far(row.remote_system_name, None, "lldp"))

    members: dict[int, list[m.Interface]] = defaultdict(list)
    for iface in interfaces:
        if iface.parent_id is not None and iface.if_type != "subinterface":
            members[iface.parent_id].append(iface)

    total_up = 0
    candidates: list[Candidate] = []
    for iface in interfaces:
        if iface.if_type not in ("physical", "bundle") or _NOT_ACCESS.match(iface.name):
            continue
        if iface.if_type == "physical" and iface.parent_id is not None:
            continue  # bundle member: the bundle is the access port
        if (iface.oper_status or "").lower() != "up" or (iface.admin_status or "up").lower() == "down":
            continue
        total_up += 1
        far = by_iface.get(iface.id)
        if far is None and iface.if_type == "bundle":
            ends = {(f.hostname.lower(), f.role) for f in (lldp.get(norm_if(mbr.name)) for mbr in members[iface.id]) if f}
            if len(ends) == 1:  # every member's LLDP names the same neighbour
                hostname, role = next(iter(ends))
                known = by_host.get(norm_host(hostname))
                far = Far(known.hostname if known else hostname, role, "lldp")
        if far is None:
            far = lldp.get(norm_if(iface.name))
        candidate = Candidate(name=iface.name, kind="bundle" if iface.if_type == "bundle" else "physical",
                              speed_gbps=iface.speed_gbps, description=iface.description,
                              members=sorted(mbr.name for mbr in members[iface.id]), far=far)
        candidate.direction = _direction(device.role, far, iface.description)
        candidates.append(candidate)

    order = {"downlink": 0, "unknown": 1, "peer": 2, "uplink": 3}
    shown = [c for c in candidates if c.direction in ("downlink", "unknown")]
    shown.sort(key=lambda c: (order[c.direction], c.kind != "bundle", c.far is None, _natural(c.name)))
    return {
        "device_id": device.id, "hostname": device.hostname, "role": device.role,
        "ports": [_as_dict(c) for c in shown],
        "hidden_uplinks": sum(c.direction in ("uplink", "peer") for c in candidates),
        "total_up": total_up,
    }


def _natural(name: str) -> list:
    """GigabitEthernet0/3/10 after 0/3/2."""
    return [int(part) if part.isdigit() else part.lower() for part in re.split(r"(\d+)", name)]


def _as_dict(c: Candidate) -> dict:
    # What the direction rests on: the neighbour (link / LLDP), else only the port description.
    basis = c.far.source if c.far and c.far.role else "description" if c.direction != "unknown" else None
    return {
        "name": c.name, "kind": c.kind, "speed_gbps": c.speed_gbps, "description": c.description,
        "members": c.members, "direction": c.direction, "basis": basis,
        "far": {"hostname": c.far.hostname, "role": c.far.role, "source": c.far.source} if c.far else None,
    }
