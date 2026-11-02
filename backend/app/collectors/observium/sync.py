"""Write Observium data into the inventory.

parts:
  inventory   devices -> POP / vendor / model / software / device, ports -> interfaces, IPv4 addresses
  neighbours  LLDP (and optionally CDP) neighbour tables, then topology discovery
  utilization current port rates -> one UtilizationSample per link + link.current_gbps

Matching is by hostname (sysName without domain) and interface name, the same keys LLDP uses,
so devices created by hand or by the seed are updated instead of duplicated. Curated fields
(device role, POP of an existing device) are never overwritten.
"""

import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from ... import models as m
from ...config import settings
from ...services.topology import discover_links, norm_host, norm_if
from .client import ObserviumClient
from .parsers import OS_VENDOR, DeviceRecord, PortRecord, parse_address, parse_device, parse_neighbour, parse_port

ALL_PARTS = frozenset({"inventory", "neighbours", "utilization"})
PORT_FIELDS = (
    "port_id,device_id,ifName,ifDescr,ifAlias,port_label_short,ifType,ifSpeed,ifHighSpeed,ifMtu,"
    "ifAdminStatus,ifOperStatus,ifInOctets_rate,ifOutOctets_rate,deleted"
)
PAGE_SIZE = 5000
SOURCE_NAME = "Observium"


@dataclass
class SyncReport:
    parts: list[str]
    counts: Counter = field(default_factory=Counter)
    warnings: list[str] = field(default_factory=list)
    discovery: dict[str, Any] | None = None
    duration_s: float = 0.0

    def warn(self, message: str, limit: int = 50) -> None:
        self.counts["warnings"] += 1
        if len(self.warnings) < limit:
            self.warnings.append(message)

    def as_dict(self) -> dict[str, Any]:
        return {
            "parts": self.parts,
            "counts": dict(self.counts),
            "warnings": self.warnings,
            "discovery": self.discovery,
            "duration_s": round(self.duration_s, 2),
        }


def _is_network_device(row: dict[str, Any]) -> bool:
    return (row.get("os") or "").lower() in OS_VENDOR or (row.get("type") or "network") == "network"


class _Writer:
    def __init__(self, db: Session, report: SyncReport):
        self.db = db
        self.report = report
        self.pops = {p.code.upper(): p for p in db.scalars(select(m.Pop))}
        self.vendors = {v.name.lower(): v for v in db.scalars(select(m.Vendor))}
        self.models = {(d.vendor_id, d.name.lower()): d for d in db.scalars(select(m.DeviceModel))}
        self.software = {(s.vendor_id, s.version.lower()): s for s in db.scalars(select(m.SoftwareVersion))}
        self.devices = {norm_host(d.hostname): d for d in db.scalars(select(m.Device))}
        self.ifaces = {(i.device_id, norm_if(i.name)): i for i in db.scalars(select(m.Interface))}

    def pop(self, rec: DeviceRecord) -> m.Pop:
        code = rec.pop_code or "UNASSIGNED"
        pop = self.pops.get(code)
        if pop is None:
            pop = m.Pop(code=code, name=rec.pop_name or code, city=rec.city,
                        latitude=rec.latitude, longitude=rec.longitude, status="active")
            self.db.add(pop)
            self.db.flush()
            self.pops[code] = pop
            self.report.counts["pops_created"] += 1
        elif pop.latitude is None and rec.latitude is not None:
            pop.latitude, pop.longitude = rec.latitude, rec.longitude
        return pop

    def vendor(self, rec: DeviceRecord) -> m.Vendor | None:
        if not rec.vendor:
            return None
        vendor = self.vendors.get(rec.vendor.lower())
        if vendor is None:
            vendor = m.Vendor(name=rec.vendor, os_family=rec.os_family)
            self.db.add(vendor)
            self.db.flush()
            self.vendors[rec.vendor.lower()] = vendor
        return vendor

    def model(self, vendor: m.Vendor | None, name: str | None) -> m.DeviceModel | None:
        if vendor is None or not name:
            return None
        key = (vendor.id, name.lower())
        if key not in self.models:
            self.models[key] = m.DeviceModel(vendor_id=vendor.id, name=name)
            self.db.add(self.models[key])
            self.db.flush()
        return self.models[key]

    def software_version(self, vendor: m.Vendor | None, version: str | None) -> m.SoftwareVersion | None:
        if vendor is None or not version:
            return None
        key = (vendor.id, version.lower())
        if key not in self.software:
            self.software[key] = m.SoftwareVersion(vendor_id=vendor.id, version=version)
            self.db.add(self.software[key])
            self.db.flush()
        return self.software[key]

    def device(self, rec: DeviceRecord) -> m.Device:
        vendor = self.vendor(rec)
        model = self.model(vendor, rec.model)
        sw = self.software_version(vendor, rec.software)
        device = self.devices.get(norm_host(rec.hostname))
        if device is None:
            device = m.Device(hostname=rec.hostname, pop_id=self.pop(rec).id, role=rec.role)
            self.db.add(device)
            self.report.counts["devices_created"] += 1
        else:
            self.report.counts["devices_updated"] += 1
        device.vendor_id = vendor.id if vendor else device.vendor_id
        device.model_id = model.id if model else device.model_id
        device.software_id = sw.id if sw else device.software_id
        device.mgmt_ip = rec.mgmt_ip or device.mgmt_ip
        device.status = rec.status
        self.db.flush()
        self.devices[norm_host(rec.hostname)] = device
        return device

    def interface(self, device: m.Device, rec: PortRecord) -> m.Interface:
        key = (device.id, norm_if(rec.name))
        iface = self.ifaces.get(key)
        if iface is None:
            iface = m.Interface(device_id=device.id, name=rec.name)
            self.db.add(iface)
            self.report.counts["interfaces_created"] += 1
        else:
            self.report.counts["interfaces_updated"] += 1
        # A config parser may already have marked this port as a bundle member/bundle; keep that.
        if iface.if_type != "bundle":
            iface.if_type = rec.if_type
        iface.speed_gbps = rec.speed_gbps if rec.speed_gbps is not None else iface.speed_gbps
        iface.mtu = rec.mtu if rec.mtu is not None else iface.mtu
        iface.admin_status = rec.admin_status
        iface.oper_status = rec.oper_status
        iface.description = rec.description or iface.description
        self.db.flush()
        self.ifaces[key] = iface
        return iface


def sync_observium(
    db: Session,
    client: ObserviumClient,
    parts: set[str] | frozenset[str] = ALL_PARTS,
    discover: bool = True,
) -> SyncReport:
    started = time.monotonic()
    parts = set(parts) & ALL_PARTS
    report = SyncReport(parts=sorted(parts))
    writer = _Writer(db, report)
    write_inventory = "inventory" in parts

    # Devices are always fetched: they map Observium ids to inventory devices for every part.
    device_by_obs: dict[str, m.Device] = {}
    for row in client.fetch("devices"):
        if not _is_network_device(row):
            continue
        rec = parse_device(row, settings.observium_pop_regex)
        if rec is None:
            report.warn(f"device {row.get('device_id')}: no hostname/sysName")
            continue
        if write_inventory:
            device_by_obs[rec.observium_id] = writer.device(rec)
        elif norm_host(rec.hostname) in writer.devices:
            device_by_obs[rec.observium_id] = writer.devices[norm_host(rec.hostname)]
    report.counts["devices_seen"] = len(device_by_obs)

    iface_by_port: dict[str, m.Interface] = {}
    rates: dict[int, tuple[float | None, float | None]] = {}
    if parts & {"inventory", "neighbours", "utilization"}:
        for row in client.fetch("ports", page_size=PAGE_SIZE, fields=PORT_FIELDS):
            rec = parse_port(row)
            device = device_by_obs.get(str(row.get("device_id")))
            if rec is None or device is None:
                continue
            if write_inventory:
                iface = writer.interface(device, rec)
            else:
                iface = writer.ifaces.get((device.id, norm_if(rec.name)))
                if iface is None:
                    continue
            iface_by_port[rec.observium_id] = iface
            rates[iface.id] = (rec.in_gbps, rec.out_gbps)
        report.counts["ports_seen"] = len(iface_by_port)

    if write_inventory:
        for row in client.fetch("address", page_size=PAGE_SIZE, af="ipv4"):
            rec = parse_address(row)
            iface = iface_by_port.get(rec.observium_port_id) if rec else None
            if rec and iface and not iface.ipv4_address:
                iface.ipv4_address = rec.cidr
                report.counts["addresses_set"] += 1

    if "neighbours" in parts:
        _write_neighbours(db, client, device_by_obs, iface_by_port, report)

    db.commit()

    if discover and parts & {"inventory", "neighbours"}:
        report.discovery = discover_links(db).model_dump()

    if "utilization" in parts:
        _write_utilization(db, rates, report)

    _record_source(db, report)
    report.duration_s = time.monotonic() - started
    return report


def _write_neighbours(db, client, device_by_obs, iface_by_port, report: SyncReport) -> None:
    tables: dict[int, list[m.LldpNeighbor]] = {d.id: [] for d in device_by_obs.values()}
    for row in client.fetch("neighbours", page_size=PAGE_SIZE):
        rec = parse_neighbour(row, include_cdp=settings.observium_include_cdp)
        if rec is None:
            continue
        device = device_by_obs.get(rec.observium_device_id)
        local = iface_by_port.get(rec.local_port_id)
        if device is None:
            continue
        if local is None:
            report.warn(f"{device.hostname}: neighbour on unknown/skipped port_id {rec.local_port_id}")
            continue
        tables[device.id].append(m.LldpNeighbor(
            device_id=device.id,
            local_interface=local.name,
            remote_system_name=rec.remote_system_name,
            remote_port_id=rec.remote_port_id,
        ))
    # Each synced device's neighbour table is replaced wholesale, mirroring Observium.
    for device_id, rows in tables.items():
        db.execute(delete(m.LldpNeighbor).where(m.LldpNeighbor.device_id == device_id))
        seen: set[str] = set()
        for n in rows:
            if norm_if(n.local_interface) in seen:  # unique per local port
                continue
            seen.add(norm_if(n.local_interface))
            db.add(n)
            report.counts["neighbours"] += 1


def _write_utilization(db: Session, rates: dict[int, tuple[float | None, float | None]], report: SyncReport) -> None:
    now = datetime.now(timezone.utc)
    for link in db.scalars(select(m.Link)):
        a = rates.get(link.a_interface_id or -1)
        b = rates.get(link.b_interface_id or -1)
        if a and a[0] is not None:
            in_g, out_g = a
        elif b and b[0] is not None:
            in_g, out_g = b[1], b[0]  # seen from the far end, directions swap
        else:
            continue
        in_g, out_g = in_g or 0.0, out_g or 0.0
        db.add(m.UtilizationSample(link_id=link.id, ts=now, in_gbps=in_g, out_gbps=out_g))
        link.current_gbps = round(max(in_g, out_g), 3)
        report.counts["utilization_samples"] += 1
    db.commit()


def _record_source(db: Session, report: SyncReport) -> None:
    source = db.scalar(select(m.IntegrationSource).where(m.IntegrationSource.name == SOURCE_NAME))
    if source is None:
        source = m.IntegrationSource(name=SOURCE_NAME, system_type="nms", target_entity="devices",
                                     base_url=settings.observium_url, auth_type="token", enabled=True)
        db.add(source)
    c = report.counts
    source.last_sync_at = datetime.now(timezone.utc)
    source.last_status = (
        f"ok: {c['devices_seen']} devices, {c['ports_seen']} ports, {c['neighbours']} neighbours, "
        f"{c['utilization_samples']} samples, {c['warnings']} warnings"
    )
    db.commit()
