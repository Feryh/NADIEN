"""Write Observium data into the inventory.

parts:
  inventory   devices -> POP / vendor / model / software / device, ports -> interfaces, IPv4 addresses
  neighbours  LLDP (and optionally CDP) neighbour tables, then topology discovery
  utilization current port rates -> one UtilizationSample per link + link.current_gbps

Matching is by hostname (sysName without domain) and interface name, the same keys LLDP uses,
so devices created by hand or by the seed are updated instead of duplicated. Curated fields
(device role, POP of an existing device) are never overwritten — except a device still parked
in the UNASSIGNED POP, which moves as soon as its POP can be inferred.

Built for ~5k devices / ~300k ports: interfaces are flushed in one batch, each phase commits on
its own, and `progress` reports the current phase to the background job runner.
"""

import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import delete, insert, select, update
from sqlalchemy.orm import Session

from ... import models as m
from ...config import settings
from ...services.areas import FUNCTION_TOKENS, csr_parts, is_city_code
from ...services.topology import discover_links, legacy_host_key, norm_host, norm_if
from .client import ObserviumClient
from .parsers import (
    OS_VENDOR,
    DeviceRecord,
    PortRecord,
    infer_pop,
    is_role_word,
    neighbour_skip_reason,
    parse_address,
    parse_device,
    parse_neighbour,
    parse_port,
)

ALL_PARTS = frozenset({"inventory", "neighbours", "utilization"})
PORT_FIELDS = (
    "port_id,device_id,ifIndex,ifName,ifDescr,ifAlias,port_label_short,ifType,ifSpeed,ifHighSpeed,ifMtu,"
    "ifAdminStatus,ifOperStatus,ifInOctets_rate,ifOutOctets_rate,deleted"
)
PAGE_SIZE = 5000
WRITE_BATCH = 20000
SOURCE_NAME = "Observium"
CSR_SOURCE_NAME = "Observium CSR"
# The CSR Observium only completes the cell-site routers: ports and traffic, no LLDP, so the topology stays as is.
CSR_PARTS = frozenset({"inventory", "utilization"})
UNASSIGNED = "UNASSIGNED"

Progress = Callable[[str], None]


@dataclass
class SyncReport:
    parts: list[str]
    counts: Counter = field(default_factory=Counter)
    warnings: list[str] = field(default_factory=list)
    diagnostics: dict[str, Any] = field(default_factory=dict)
    discovery: dict[str, Any] | None = None
    duration_s: float = 0.0
    # Ports the NMS confirmed this run; a caller running discovery itself needs them for phantom-port cleanup.
    valid_iface_ids: set[int] | None = field(default=None, repr=False)

    def warn(self, message: str, limit: int = 50) -> None:
        self.counts["warnings"] += 1
        if len(self.warnings) < limit:
            self.warnings.append(message)

    def as_dict(self) -> dict[str, Any]:
        return {
            "parts": self.parts,
            "counts": dict(self.counts),
            "warnings": self.warnings,
            "diagnostics": self.diagnostics,
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
        code = rec.pop_code or UNASSIGNED
        pop = self.pops.get(code)
        if pop is None:
            pop = m.Pop(code=code, name=rec.pop_name if code != UNASSIGNED and rec.pop_name else code,
                        city=rec.city, latitude=rec.latitude, longitude=rec.longitude, status="active")
            self.db.add(pop)
            self.db.flush()
            self.pops[code] = pop
            self.report.counts["pops_created"] += 1
        elif pop.latitude is None and rec.latitude is not None and code != UNASSIGNED:
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
        if device is None and "." in rec.hostname:
            # Stored under the old cut-at-first-dot key ("R6" for R6.BAM.LEAF-CNDC.02): rename it.
            device = self.devices.pop(legacy_host_key(rec.hostname), None)
            if device is not None:
                device.hostname = rec.hostname
                self.report.counts["devices_renamed"] += 1
        if device is None:
            device = m.Device(hostname=rec.hostname, pop_id=self.pop(rec).id, role=rec.role)
            self.db.add(device)
            self.report.counts["devices_created"] += 1
        else:
            self.report.counts["devices_updated"] += 1
            # Not curated: parked in UNASSIGNED, in a POP an older parser named after a role or function
            # word (OLT, NAP), or in a non-city POP while the hostname names a registered city code.
            current = next((p for p in self.pops.values() if p.id == device.pop_id), None)
            misplaced = (current is None or current.code == UNASSIGNED or is_role_word(current.code)
                         or current.code in FUNCTION_TOKENS
                         or (not is_city_code(current.code) and is_city_code(rec.pop_code)))
            # A POP chosen by hand (pop_locked) is kept, whatever the hostname says.
            if rec.pop_code and misplaced and not device.pop_locked and (current is None or current.code != rec.pop_code):
                device.pop_id = self.pop(rec).id
                self.report.counts["devices_pop_assigned"] += 1
        device.vendor_id = vendor.id if vendor else device.vendor_id
        device.model_id = model.id if model else device.model_id
        device.software_id = sw.id if sw else device.software_id
        device.mgmt_ip = rec.mgmt_ip or device.mgmt_ip
        device.status = rec.status
        device.reachability = rec.reachability
        device.uptime_s = rec.uptime_s
        device.last_polled_at = rec.last_polled_at
        self.db.flush()
        self.devices[norm_host(rec.hostname)] = device
        return device

    def interface(self, device: m.Device, rec: PortRecord) -> m.Interface:
        """Upsert without flushing; the caller flushes once for all ports."""
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
        iface.mib_type = rec.mib_type or iface.mib_type
        iface.speed_gbps = rec.speed_gbps if rec.speed_gbps is not None else iface.speed_gbps
        iface.mtu = rec.mtu if rec.mtu is not None else iface.mtu
        iface.admin_status = rec.admin_status
        iface.oper_status = rec.oper_status
        iface.description = rec.description or iface.description
        self.ifaces[key] = iface
        return iface


def _device_records(rows: list[dict[str, Any]], known_pops: set[str], report: SyncReport) -> list[DeviceRecord]:
    """Parse devices in two passes: regex POPs first, then infer the rest from the known POP codes."""
    records = []
    for row in rows:
        if not _is_network_device(row):
            continue
        rec = parse_device(row, settings.observium_pop_regex)
        if rec is None:
            report.warn(f"device {row.get('device_id')}: no hostname/sysName")
            continue
        records.append(rec)
    known = {c for c in known_pops | {r.pop_code for r in records if r.pop_code} if not is_role_word(c)} - {UNASSIGNED}
    for rec in records:
        if rec.pop_code is None:
            rec.pop_code = infer_pop(rec.hostname, known)
            if rec.pop_code:
                report.counts["pops_inferred"] += 1
    report.counts["devices_unassigned"] = sum(1 for r in records if not r.pop_code)
    return records


def sync_observium(
    db: Session,
    client: ObserviumClient,
    parts: set[str] | frozenset[str] = ALL_PARTS,
    discover: bool = True,
    progress: Progress | None = None,
    source: str = "main",
) -> SyncReport:
    """`source="csr"`: the second Observium. It also monitors servers and other boxes; only cell-site routers count."""
    started = time.monotonic()
    step = progress or (lambda _phase: None)
    parts = set(parts) & ALL_PARTS
    report = SyncReport(parts=sorted(parts))
    writer = _Writer(db, report)
    write_inventory = "inventory" in parts

    # Devices are always fetched: they map Observium ids to inventory devices for every part.
    step("devices")
    device_by_obs: dict[str, m.Device] = {}
    obs_hostname: dict[str, str] = {}
    for rec in _device_records(client.fetch("devices"), set(writer.pops), report):
        if source == "csr" and not _prepare_csr(rec, writer):
            report.counts["devices_skipped_not_csr"] += 1
            continue
        obs_hostname[rec.observium_id] = rec.observium_hostname
        if write_inventory:
            device_by_obs[rec.observium_id] = writer.device(rec)
        elif norm_host(rec.hostname) in writer.devices:
            device_by_obs[rec.observium_id] = writer.devices[norm_host(rec.hostname)]
    report.counts["devices_seen"] = len(device_by_obs)
    if write_inventory:
        db.commit()

    step("ports")
    iface_by_port: dict[str, m.Interface] = {}
    rates_by_port: dict[str, tuple[float | None, float | None]] = {}
    rrd_ref: dict[str, tuple[int | None, str]] = {}  # observium port id -> (ifIndex, Observium hostname)
    pending = 0
    for row in client.fetch("ports", page_size=PAGE_SIZE, fields=PORT_FIELDS):
        rec = parse_port(row)
        device = device_by_obs.get(str(row.get("device_id")))
        if rec is None or device is None:
            continue
        rrd_ref[rec.observium_id] = (rec.if_index, obs_hostname.get(rec.observium_device_id, ""))
        if write_inventory:
            iface = writer.interface(device, rec)
            pending += 1
            if pending >= WRITE_BATCH:
                # Short write transactions: other writers (UI edits) never wait long for the lock.
                db.commit()
                pending = 0
        else:
            iface = writer.ifaces.get((device.id, norm_if(rec.name)))
            if iface is None:
                continue
        iface_by_port[rec.observium_id] = iface
        rates_by_port[rec.observium_id] = (rec.in_gbps, rec.out_gbps)
    db.flush()  # ids for the last batch of new interfaces
    rates = {iface_by_port[p].id: r for p, r in rates_by_port.items()}
    report.counts["ports_seen"] = len(iface_by_port)
    _write_rrd_map(db, iface_by_port, rrd_ref, source)

    if write_inventory:
        step("addresses")
        for row in client.fetch("address", page_size=PAGE_SIZE, af="ipv4"):
            rec = parse_address(row)
            iface = iface_by_port.get(rec.observium_port_id) if rec else None
            if rec and iface and not iface.ipv4_address:
                iface.ipv4_address = rec.cidr
                report.counts["addresses_set"] += 1
        db.commit()

    if "neighbours" in parts:
        step("neighbours")
        _write_neighbours(db, client, device_by_obs, iface_by_port, report)
        db.commit()

    if parts & {"inventory", "neighbours"}:
        report.valid_iface_ids = {i.id for i in iface_by_port.values()}
    if discover and report.valid_iface_ids is not None:
        step("discovery")
        report.discovery = discover_links(db, valid_iface_ids=report.valid_iface_ids).model_dump()

    if "utilization" in parts:
        step("utilization")
        _write_utilization(db, rates, report)

    report.diagnostics["observium_responses"] = client.meta
    _record_source(db, report, source)
    report.duration_s = time.monotonic() - started
    return report


def _prepare_csr(rec: DeviceRecord, writer: "_Writer") -> bool:
    """Whether a device of the CSR Observium is a cell-site router; new ones get the inventory's upper-case name."""
    existing = writer.devices.get(norm_host(rec.hostname))
    if existing is not None:
        return existing.role == "csr"
    if not csr_parts(rec.hostname):
        return False
    rec.hostname = rec.hostname.upper()
    return True


def _write_rrd_map(db: Session, iface_by_port: dict[str, m.Interface],
                   rrd_ref: dict[str, tuple[int | None, str]], source: str = "main") -> None:
    """Remember where each interface's RRD lives (rebuilt every sync — ifIndex can change on reboot). Only this
    server's rows are rebuilt: the other Observium's ports stay mapped."""
    rows, seen = [], set()
    for port_id, iface in iface_by_port.items():
        if_index, hostname = rrd_ref.get(port_id, (None, ""))
        if iface.id in seen or not hostname:
            continue
        seen.add(iface.id)
        rows.append({"interface_id": iface.id, "observium_port_id": int(port_id), "if_index": if_index,
                     "observium_hostname": hostname, "source": source})
    db.execute(delete(m.ObserviumPort).where(m.ObserviumPort.source == source))
    for i in range(0, len(rows), WRITE_BATCH):
        batch = rows[i:i + WRITE_BATCH]
        # A device on both servers: the last server synced owns its ports.
        db.execute(delete(m.ObserviumPort).where(m.ObserviumPort.interface_id.in_([r["interface_id"] for r in batch])))
        db.execute(insert(m.ObserviumPort), batch)
    db.commit()


def _write_neighbours(db, client, device_by_obs, iface_by_port, report: SyncReport) -> None:
    """Store neighbour tables with the far end resolved as precisely as possible:

    1. Observium's remote_port_id -> our interface for that port: exact device + canonical port
       name ("GigabitEthernet0/0/1", not the "Gi0/0/1" LLDP advertised). Avoids duplicate ports
       and links paired to the wrong port.
    2. remote_address = a device's management IP -> that device's hostname (port name as advertised).
    3. Otherwise the advertised system name/port, matched by hostname during discovery.
    LLDP wins over CDP when both report the same local port.
    """
    tables: dict[int, list[tuple[int, m.LldpNeighbor]]] = {d.id: [] for d in device_by_obs.values()}
    devices_by_id = {d.id: d for d in device_by_obs.values()}
    devices_by_ip = {d.mgmt_ip: d for d in device_by_obs.values() if d.mgmt_ip}
    protocols: Counter = Counter()
    skipped: Counter = Counter()
    resolved: Counter = Counter()
    sample_keys: list[str] = []
    fetched = 0
    include_cdp = settings.observium_include_cdp
    for row in client.fetch("neighbours", page_size=PAGE_SIZE):
        fetched += 1
        if not sample_keys:
            sample_keys = sorted(row.keys())
        protocols[str(row.get("protocol") or "none").lower()] += 1
        reason = neighbour_skip_reason(row, include_cdp)
        if reason:
            skipped[reason] += 1
            continue
        rec = parse_neighbour(row, include_cdp=include_cdp)
        device = device_by_obs.get(rec.observium_device_id)
        local = iface_by_port.get(rec.local_port_id)
        if device is None:
            skipped["device_not_synced"] += 1
            continue
        if local is None:
            skipped["port_not_synced"] += 1
            report.warn(f"{device.hostname}: neighbour on unknown/skipped port_id {rec.local_port_id}")
            continue

        remote_name, remote_port = rec.remote_system_name, rec.remote_port_id
        remote_iface = iface_by_port.get(rec.remote_observium_port_id or "")
        if remote_iface is not None and remote_iface.device_id in devices_by_id:
            remote_name, remote_port = devices_by_id[remote_iface.device_id].hostname, remote_iface.name
            resolved["by_observium_port_id"] += 1
        elif rec.remote_address in devices_by_ip:
            remote_name = devices_by_ip[rec.remote_address].hostname
            resolved["by_management_ip"] += 1
        else:
            resolved["by_advertised_name"] += 1

        priority = 0 if rec.protocol == "lldp" else 1
        tables[device.id].append((priority, m.LldpNeighbor(
            device_id=device.id,
            local_interface=local.name,
            remote_system_name=remote_name,
            remote_port_id=remote_port,
        )))
    report.diagnostics["neighbours"] = {
        "fetched": fetched,
        "protocols": dict(protocols),
        "skipped": dict(skipped),
        "resolved": dict(resolved),
        "row_fields": sample_keys,
    }
    # Each synced device's neighbour table is replaced wholesale, mirroring Observium.
    db.execute(delete(m.LldpNeighbor).where(m.LldpNeighbor.device_id.in_(list(tables))))
    for rows in tables.values():
        seen: set[str] = set()
        for _, n in sorted(rows, key=lambda r: r[0]):  # LLDP first
            if norm_if(n.local_interface) in seen:  # unique per local port
                continue
            seen.add(norm_if(n.local_interface))
            db.add(n)
            report.counts["neighbours"] += 1


def _write_utilization(db: Session, rates: dict[int, tuple[float | None, float | None]], report: SyncReport) -> None:
    now = datetime.now(timezone.utc)
    if rates:
        db.execute(update(m.Interface), [{"id": iface_id, "in_gbps": in_g, "out_gbps": out_g}
                                         for iface_id, (in_g, out_g) in rates.items()])
    speeds = dict(db.execute(select(m.Interface.id, m.Interface.speed_gbps).where(
        m.Interface.id.in_(select(m.Link.a_interface_id).union(select(m.Link.b_interface_id))))).tuples().all())
    for link in db.scalars(select(m.Link)):
        if link.status == "down":
            # A link discovery no longer sees often shares a port with the link that replaced it (a renamed far
            # end): the far end's traffic belongs to that live link, not to this one.
            link.current_gbps = 0.0
            continue
        a = rates.get(link.a_interface_id or -1)
        b = rates.get(link.b_interface_id or -1)
        a_ok, b_ok = bool(a and a[0] is not None), bool(b and b[0] is not None)
        if a_ok and b_ok and _narrower(speeds.get(link.b_interface_id), speeds.get(link.a_interface_id)):
            # A bundle whose members lead to several boxes (smartgroup210: one member to this peer, one elsewhere)
            # carries more than this link: the narrower end only carries what goes over it.
            a_ok = False
        if a_ok:
            in_g, out_g = a
        elif b_ok:
            in_g, out_g = b[1], b[0]  # seen from the far end, directions swap
        else:
            continue
        in_g, out_g = in_g or 0.0, out_g or 0.0
        db.add(m.UtilizationSample(link_id=link.id, ts=now, in_gbps=in_g, out_gbps=out_g))
        link.current_gbps = round(max(in_g, out_g), 3)
        report.counts["utilization_samples"] += 1
    db.commit()


def _narrower(speed: float | None, other: float | None) -> bool:
    return bool(speed and other and speed < other)


def _record_source(db: Session, report: SyncReport, kind: str = "main") -> None:
    name, url = (CSR_SOURCE_NAME, settings.observium_csr_url) if kind == "csr" else (SOURCE_NAME, settings.observium_url)
    source = db.scalar(select(m.IntegrationSource).where(m.IntegrationSource.name == name))
    if source is None:
        source = m.IntegrationSource(name=name, system_type="nms", target_entity="devices",
                                     base_url=url, auth_type="basic", enabled=True)
        db.add(source)
    c = report.counts
    source.last_sync_at = datetime.now(timezone.utc)
    source.last_status = (
        f"ok: {c['devices_seen']} devices, {c['ports_seen']} ports, {c['neighbours']} neighbours, "
        f"{c['utilization_samples']} samples, {c['warnings']} warnings"
    )
    db.commit()
