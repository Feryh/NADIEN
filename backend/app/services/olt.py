"""OLT PON ports as Observium reports them, for GPON port occupancy.

Huawei, ZTE and FiberHome OLTs all show their PON ports as `Slot<n>/Port<m>` with the ONT counters in
the description (`FoCut:0 online:18 DyingGasp:4 Deactivated`); uplinks are named after the card
(`xgei_1/10/4`, `SFP+ 9/4`, `ethernet0/9/1`).

Observium lists only PON ports in service, so nearly every one is up: "ports used / ports" is ~100 % on every
OLT. A PON port fills up with ONTs instead, so its occupancy is online ONTs / NADIEN_PON_MAX_ONTS (split ratio).
"""

import re
from dataclasses import dataclass

from ..config import settings
from .capacity import hotspot_level, occupancy_pct

_PON_NAME = re.compile(r"^slot(\d+)/port(\d+)$", re.IGNORECASE)
_MGMT = re.compile(r"^(meth|mgmt|mgt|eth0$|console|null|loopback|vlan)", re.IGNORECASE)
_ONLINE = re.compile(r"\bonline:(\d+)", re.IGNORECASE)
_HOT = ("warning", "critical")


def is_pon_port(name: str, description: str | None) -> bool:
    return bool(_PON_NAME.match(name.strip())) or bool(description and _ONLINE.search(description))


def onts_online(description: str | None) -> int:
    match = _ONLINE.search(description or "")
    return int(match.group(1)) if match else 0


def pon_slot_port(name: str) -> tuple[int, int] | None:
    """(slot, port) of a `Slot<n>/Port<m>` PON port."""
    match = _PON_NAME.match(name.strip())
    return (int(match.group(1)), int(match.group(2))) if match else None


def is_management_port(name: str) -> bool:
    return bool(_MGMT.match(name.strip()))


def pon_port_occupancy(onts: int) -> float:
    return occupancy_pct(onts, settings.pon_max_onts)


def pon_level(pct: float) -> str:
    # More ONTs than the split ratio is an overfull port (or a 1:128 split), never bad data like a link's 'suspect'.
    return hotspot_level(min(pct, 100.0))


def olt_layout(ports: list[dict]) -> dict:
    """Port ids of an OLT as its front panel reads: uplink ports, then PON ports per slot in port order."""
    slots: dict[int, list[tuple[int, int]]] = {}
    uplinks = []
    for p in ports:
        if p["if_type"] != "physical":
            continue
        slot_port = pon_slot_port(p["name"]) if p["pon"] else None
        if slot_port:
            slots.setdefault(slot_port[0], []).append((slot_port[1], p["id"]))
        elif not p["pon"] and not is_management_port(p["name"]):
            uplinks.append(p)
    uplinks.sort(key=lambda p: (p["link"] is None, p["state"] != "used", p["name"]))
    return {"uplinks": [p["id"] for p in uplinks],
            "slots": [{"slot": s, "ports": [pid for _, pid in sorted(slots[s])]} for s in sorted(slots)]}


@dataclass
class PonUsage:
    """Enabled PON ports of one OLT: how many carry ONTs, and how full the fullest ones are."""

    total: int = 0
    used: int = 0
    onts: int = 0
    busiest_onts: int = 0
    hot_ports: int = 0  # PON ports at or above the warning threshold

    def add(self, admin: str | None, oper: str | None, description: str | None) -> None:
        if (admin or "").lower() == "down":
            return
        online = onts_online(description)
        self.total += 1
        self.onts += online
        self.busiest_onts = max(self.busiest_onts, online)
        if online > 0 or (oper or "").lower() == "up":
            self.used += 1
        if pon_level(pon_port_occupancy(online)) in _HOT:
            self.hot_ports += 1

    @property
    def busiest_pct(self) -> float:
        return pon_port_occupancy(self.busiest_onts)
