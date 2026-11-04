"""Naming compliance of the whole inventory: one row per device and one per link end (port description)."""

from collections import Counter
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models as m
from .areas import site_for
from .naming import (DESCRIPTION_STATUS_LABELS, HOSTNAME_STATUS_LABELS, PortFix, check_hostname, description_script,
                     description_status, recommended_description)
from .segments import SEGMENT_LABELS, link_segment
from .vendors import DIALECT_LABELS, dialect_for


@dataclass
class Inventory:
    """What the report needs from the topology context: devices with their POP code, area and vendor."""

    devices: dict[int, Any]  # rows: id, hostname, pop_id, role, vendor_id
    pop_code: dict[int, str]  # device id -> POP code
    area: dict[int, str]  # device id -> area
    vendors: dict[int, Any]  # vendor id -> row with name, os_family


def device_rows(inv: Inventory) -> list[dict[str, Any]]:
    rows = []
    for d in inv.devices.values():
        vendor = inv.vendors.get(d.vendor_id)
        pop = inv.pop_code.get(d.id)
        check = check_hostname(d.hostname, d.role, pop, vendor.name if vendor else None, site_for(d.hostname, pop or ""))
        rows.append({"device_id": d.id, "hostname": d.hostname, "role": d.role, "pop": pop, "area": inv.area.get(d.id),
                     "vendor": vendor.name if vendor else None, "convention": check.convention, "status": check.status,
                     "status_label": HOSTNAME_STATUS_LABELS[check.status], "issues": check.issues,
                     "suggestion": check.suggestion})
    return sorted(rows, key=lambda r: (r["status"] == "standard", r["area"] or "", r["hostname"]))


def port_rows(db: Session, inv: Inventory) -> list[dict[str, Any]]:
    links = db.execute(select(m.Link.id, m.Link.a_device_id, m.Link.a_interface_id, m.Link.b_device_id,
                              m.Link.b_interface_id, m.Link.capacity_gbps, m.Link.status)).all()
    wanted = {i for link in links for i in (link.a_interface_id, link.b_interface_id) if i}
    ifaces = {r.id: r for r in db.execute(select(m.Interface.id, m.Interface.name, m.Interface.description)
                                          .where(m.Interface.id.in_(wanted)))} if wanted else {}
    rows = []
    for link in links:
        if link.a_device_id not in inv.devices or link.b_device_id not in inv.devices:
            continue
        a, b = inv.devices[link.a_device_id], inv.devices[link.b_device_id]
        segment = link_segment(a.role, b.role)
        for mine, iface_id, peer, peer_iface_id in ((a, link.a_interface_id, b, link.b_interface_id),
                                                    (b, link.b_interface_id, a, link.a_interface_id)):
            iface = ifaces.get(iface_id)
            if iface is None:
                continue
            peer_iface = ifaces.get(peer_iface_id)
            status = description_status(iface.description, peer.hostname)
            rows.append({
                "link_id": link.id, "device_id": mine.id, "hostname": mine.hostname, "area": inv.area.get(mine.id),
                "port": iface.name, "description": iface.description, "peer": peer.hostname,
                "peer_port": peer_iface.name if peer_iface else None, "segment": segment,
                "segment_label": SEGMENT_LABELS[segment], "capacity_gbps": link.capacity_gbps,
                "link_status": link.status, "status": status, "status_label": DESCRIPTION_STATUS_LABELS[status],
                "recommended": recommended_description(segment, peer.hostname, peer_iface.name if peer_iface else None,
                                                       link.capacity_gbps),
            })
    return sorted(rows, key=lambda r: (r["status"] == "standard", r["hostname"], r["port"]))


def summary(devices: list[dict[str, Any]], ports: list[dict[str, Any]]) -> dict[str, Any]:
    by_area: dict[str, Counter] = {}
    for row in devices:
        by_area.setdefault(row["area"] or "-", Counter())[row["status"]] += 1
    return {
        "devices": {"total": len(devices), "status": dict(Counter(r["status"] for r in devices)),
                    "conventions": dict(Counter(r["convention"] or "none" for r in devices))},
        "ports": {"total": len(ports), "status": dict(Counter(r["status"] for r in ports)),
                  "segments": {seg: dict(Counter(r["status"] for r in ports if r["segment"] == seg))
                               for seg in SEGMENT_LABELS}},
        "areas": [{"area": area, "total": sum(c.values()), **dict(c)} for area, c in sorted(by_area.items())],
        "status_labels": {"devices": HOSTNAME_STATUS_LABELS, "ports": DESCRIPTION_STATUS_LABELS},
    }


def device_script(ports: list[dict[str, Any]], device_id: int, inv: Inventory) -> dict[str, Any]:
    """Description fixes of one device's link ports, in its own CLI syntax (for review / a change request)."""
    device = inv.devices[device_id]
    vendor = inv.vendors.get(device.vendor_id)
    dialect = dialect_for(vendor.name if vendor else None, vendor.os_family if vendor else None, device.hostname)
    fixes = [PortFix(r["port"], r["recommended"], r["description"])
             for r in ports if r["device_id"] == device_id and r["status"] != "standard"]
    script = description_script(dialect, fixes)
    return {"device_id": device_id, "hostname": device.hostname, "dialect": dialect,
            "dialect_label": DIALECT_LABELS.get(dialect or "", "Vendor tidak didukung generator"),
            "fixes": [f.__dict__ for f in fixes],
            "config": script["config"] if script else [], "rollback": script["rollback"] if script else []}
