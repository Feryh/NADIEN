"""Service validation (Phase 5): engineering checks on a service design before it is activated.

Four groups, each check pass / warn / fail / info:
  kapasitas  — every primary link has room (warning ≥ 70 %, critical ≥ 90 % after the service), no link
               down; a protected service has a backup sharing no link, no fiber (SRLG) and ideally no
               transit node with the primary.
  port       — access ports exist, are enabled, are not uplinks; MTU along the path fits the service.
  vlan       — VLAN present where needed and not taken on the same port; L3VPN has VRF/RD/RT, RD/RT
               well-formed and not reused by another VRF.
  perangkat  — devices on the path are active, have loopback and IS-IS NET, run recommended software.

The same engine validates a stored service and a design that is not reserved yet.
"""

import re
from dataclasses import dataclass, field
from typing import Any, Literal

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from .. import models as m
from ..config import settings
from .capacity import hotspot_level

Status = Literal["pass", "warn", "fail", "info"]
GROUPS = {
    "kapasitas": "Kapasitas & redundansi",
    "port": "MTU & port",
    "vlan": "VLAN / VRF / RD / RT",
    "perangkat": "Kesiapan perangkat",
}
# Smallest MTU a core link must carry for the service (customer frame/packet + MPLS/VLAN overhead),
# and the MTU recommended for the core so any service fits (jumbo).
REQUIRED_CORE_MTU = {"l2vpn": 1600, "iplc": 1600, "l3vpn": 1530, "internet": 1530}
RECOMMENDED_CORE_MTU = 9000
ACCESS_MIN_MTU = 1500
# RD / RT: ASN:NN (2- or 4-byte ASN) or IPv4:NN.
_RD_RT = re.compile(r"^(\d{1,10}|\d{1,3}(\.\d{1,3}){3}):\d{1,10}$")


@dataclass
class ServiceDesign:
    service_type: str
    bandwidth_mbps: int
    protection: str = "none"
    a_device_id: int | None = None
    a_interface: str | None = None
    b_device_id: int | None = None
    b_interface: str | None = None
    vlan_id: int | None = None
    vrf_name: str | None = None
    route_distinguisher: str | None = None
    route_target: str | None = None
    primary_link_ids: list[int] = field(default_factory=list)
    backup_link_ids: list[int] = field(default_factory=list)
    service_id: int | None = None  # a stored service: excluded from conflict checks
    already_reserved: bool = False  # its bandwidth is already in the links' reserved_gbps

    @property
    def bandwidth_gbps(self) -> float:
        return self.bandwidth_mbps / 1000


@dataclass
class Check:
    group: str
    code: str
    status: Status
    title: str
    detail: str = ""
    items: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {"group": self.group, "code": self.code, "status": self.status, "title": self.title,
                "detail": self.detail, "items": self.items[:50]}


class _Context:
    """Everything the checks read, loaded once."""

    def __init__(self, db: Session, design: ServiceDesign):
        self.db, self.design = db, design
        ids = set(design.primary_link_ids) | set(design.backup_link_ids)
        self.links = {l.id: l for l in db.scalars(select(m.Link).where(m.Link.id.in_(ids)))} if ids else {}
        device_ids = {d for l in self.links.values() for d in (l.a_device_id, l.b_device_id)}
        device_ids |= {d for d in (design.a_device_id, design.b_device_id) if d}
        self.devices = {d.id: d for d in db.scalars(select(m.Device).where(m.Device.id.in_(device_ids)))} if device_ids else {}
        iface_ids = {i for l in self.links.values() for i in (l.a_interface_id, l.b_interface_id) if i}
        self.ifaces = {i.id: i for i in db.scalars(select(m.Interface).where(m.Interface.id.in_(iface_ids)))} if iface_ids else {}
        software_ids = {d.software_id for d in self.devices.values() if d.software_id}
        self.software = ({s.id: s for s in db.scalars(select(m.SoftwareVersion).where(m.SoftwareVersion.id.in_(software_ids)))}
                         if software_ids else {})
        self.vendors_with_recommendation = set(db.scalars(
            select(m.SoftwareVersion.vendor_id).where(m.SoftwareVersion.is_recommended.is_(True))))
        others = select(m.Service).where(m.Service.status != "terminated")
        if design.service_id is not None:
            others = others.where(m.Service.id != design.service_id)
        self.other_services = db.scalars(others).all()

    def hostname(self, device_id: int | None) -> str:
        device = self.devices.get(device_id) if device_id else None
        return device.hostname if device else f"#{device_id}"

    def path_devices(self, link_ids: list[int]) -> list[int]:
        out: list[int] = []
        for lid in link_ids:
            link = self.links.get(lid)
            if link:
                for d in (link.a_device_id, link.b_device_id):
                    if d not in out:
                        out.append(d)
        return out


# --------------------------------------------------------------------------- kapasitas & redundansi


def _capacity(ctx: _Context) -> list[Check]:
    d = ctx.design
    if not d.primary_link_ids:
        return [Check("kapasitas", "CAP-PATH", "fail", "Belum ada jalur primary",
                      "Hitung dan pilih jalur di Customer Path dulu.")]
    checks = []
    down = [ctx.links[l].name for l in d.primary_link_ids if l in ctx.links and ctx.links[l].status != "up"]
    missing = [str(l) for l in d.primary_link_ids if l not in ctx.links]
    if down or missing:
        checks.append(Check("kapasitas", "CAP-DOWN", "fail", "Link primary down atau tidak ada",
                            f"{len(down)} down, {len(missing)} sudah tidak ada", down + [f"link #{l}" for l in missing]))
    worst: dict[str, list[str]] = {"critical": [], "warning": [], "suspect": [], "unknown": []}
    peak = 0.0
    for lid in d.primary_link_ids:
        link = ctx.links.get(lid)
        if link is None:
            continue
        if not link.capacity_gbps:
            worst["unknown"].append(link.name)
            continue
        added = 0.0 if d.already_reserved else d.bandwidth_gbps
        after = ((link.current_gbps or 0) + (link.reserved_gbps or 0) + added) / link.capacity_gbps * 100
        peak = max(peak, after)
        level = hotspot_level(after)
        if level in worst:
            worst[level].append(f"{link.name} — {after:.0f}% setelah layanan")
    thresholds = f"warning ≥ {settings.hotspot_warning_pct:g}%, critical ≥ {settings.hotspot_critical_pct:g}%"
    if worst["critical"]:
        checks.append(Check("kapasitas", "CAP-UTIL", "fail", "Link primary melewati batas critical", thresholds, worst["critical"]))
    elif worst["warning"]:
        checks.append(Check("kapasitas", "CAP-UTIL", "warn", "Link primary di atas batas warning", thresholds, worst["warning"]))
    else:
        checks.append(Check("kapasitas", "CAP-UTIL", "pass", "Kapasitas link primary cukup",
                            f"Utilisasi tertinggi setelah layanan {peak:.0f}% ({thresholds})"))
    if worst["suspect"] or worst["unknown"]:
        checks.append(Check("kapasitas", "CAP-DATA", "warn", "Kapasitas sebagian link tidak bisa dipercaya",
                            "Kapasitas 0 atau utilisasi jauh di atas 100% (speed port dari SNMP kemungkinan salah)",
                            worst["suspect"] + worst["unknown"]))
    checks.extend(_redundancy(ctx))
    return checks


def _redundancy(ctx: _Context) -> list[Check]:
    d = ctx.design
    if d.protection == "none":
        return [Check("kapasitas", "CAP-PROT", "info", "Layanan tanpa proteksi", "Proteksi tidak diminta (none).")]
    if not d.backup_link_ids:
        return [Check("kapasitas", "CAP-PROT", "fail", "Proteksi diminta tetapi tidak ada jalur backup",
                      f"Proteksi {d.protection} membutuhkan jalur backup.")]
    checks = []
    shared_links = set(d.primary_link_ids) & set(d.backup_link_ids)
    if shared_links:
        checks.append(Check("kapasitas", "CAP-DIV-LINK", "fail", "Backup memakai link yang sama dengan primary", "",
                            [ctx.links[l].name for l in shared_links if l in ctx.links]))
    p_fibers = {ctx.links[l].fiber_id for l in d.primary_link_ids if l in ctx.links and ctx.links[l].fiber_id}
    b_fibers = {ctx.links[l].fiber_id for l in d.backup_link_ids if l in ctx.links and ctx.links[l].fiber_id}
    if p_fibers & b_fibers:
        checks.append(Check("kapasitas", "CAP-SRLG", "fail", "Primary dan backup lewat fiber yang sama (SRLG)",
                            f"Fiber bersama: {', '.join(f'#{f}' for f in sorted(p_fibers & b_fibers))}"))
    elif not p_fibers or not b_fibers:
        checks.append(Check("kapasitas", "CAP-SRLG", "warn", "Risiko fiber bersama (SRLG) belum bisa diperiksa",
                            "Link di jalur belum terhubung ke data fiber."))
    else:
        checks.append(Check("kapasitas", "CAP-SRLG", "pass", "Primary dan backup tidak berbagi fiber"))
    primary, backup = ctx.path_devices(d.primary_link_ids), ctx.path_devices(d.backup_link_ids)
    transit = set(primary[1:-1]) & set(backup[1:-1])
    if transit:
        checks.append(Check("kapasitas", "CAP-DIV-NODE", "warn", "Backup melewati perangkat transit yang sama", "",
                            [ctx.hostname(x) for x in sorted(transit)]))
    elif not shared_links:
        checks.append(Check("kapasitas", "CAP-DIV-NODE", "pass", "Primary dan backup tidak berbagi perangkat transit"))
    if primary and backup and (primary[0] == backup[0] or primary[-1] == backup[-1]):
        checks.append(Check("kapasitas", "CAP-HOMING", "warn", "Single-homed di salah satu ujung",
                            "Primary dan backup berakhir di perangkat yang sama; gangguan perangkat itu memutus keduanya."))
    return checks


# --------------------------------------------------------------------------- MTU & port


def _ports(ctx: _Context) -> list[Check]:
    d = ctx.design
    checks: list[Check] = []
    uplinks = set()
    for a_dev, a_if, b_dev, b_if in ctx.db.execute(select(m.Link.a_device_id, m.Link.a_interface_id,
                                                          m.Link.b_device_id, m.Link.b_interface_id)):
        uplinks.update({(a_dev, a_if), (b_dev, b_if)})
    for side, device_id, name in (("A", d.a_device_id, d.a_interface), ("B", d.b_device_id, d.b_interface)):
        if device_id is None:
            checks.append(Check("port", f"PORT-{side}", "fail", f"Perangkat ujung {side} belum ditentukan"))
            continue
        host = ctx.hostname(device_id)
        if not name:
            checks.append(Check("port", f"PORT-{side}", "warn", f"Port akses {side} belum ditentukan",
                                f"Pilih port di {host} sebelum implementasi."))
            continue
        iface = ctx.db.scalar(select(m.Interface).where(m.Interface.device_id == device_id,
                                                        m.Interface.name == name))
        if iface is None:
            checks.append(Check("port", f"PORT-{side}", "fail", f"Port {side} tidak ada di inventory", f"{host} {name}"))
            continue
        problems = []
        if iface.admin_status == "down":
            problems.append("admin down (shutdown)")
        if (device_id, iface.id) in uplinks:
            problems.append("dipakai sebagai uplink/link backbone")
        if iface.mtu is not None and iface.mtu < ACCESS_MIN_MTU:
            problems.append(f"MTU {iface.mtu} < {ACCESS_MIN_MTU}")
        if problems:
            checks.append(Check("port", f"PORT-{side}", "fail", f"Port {side} tidak layak: {host} {name}", "; ".join(problems)))
        else:
            checks.append(Check("port", f"PORT-{side}", "pass", f"Port {side} siap: {host} {name}",
                                f"{iface.speed_gbps:g}G" if iface.speed_gbps else "speed tidak diketahui"))
    checks.append(_mtu(ctx))
    return checks


def _mtu(ctx: _Context) -> Check:
    d = ctx.design
    required = REQUIRED_CORE_MTU.get(d.service_type, 1530)
    known, unknown, low, below_jumbo = [], 0, [], []
    for lid in d.primary_link_ids + d.backup_link_ids:
        link = ctx.links.get(lid)
        if link is None:
            continue
        for iid in (link.a_interface_id, link.b_interface_id):
            iface = ctx.ifaces.get(iid) if iid else None
            if iface is None or iface.mtu is None:
                unknown += 1
                continue
            known.append(iface.mtu)
            label = f"{ctx.hostname(iface.device_id)} {iface.name} — MTU {iface.mtu}"
            if iface.mtu < required:
                low.append(label)
            elif iface.mtu < RECOMMENDED_CORE_MTU:
                below_jumbo.append(label)
    if low:
        return Check("port", "MTU-CORE", "fail", f"MTU di jalur di bawah kebutuhan {d.service_type} ({required})", "", low)
    if not known:
        return Check("port", "MTU-CORE", "warn", "MTU link di jalur belum diketahui",
                     f"{unknown} port tanpa data MTU; jalankan sync config (RANCID/gitweb).")
    detail = f"MTU terendah {min(known)} (butuh ≥ {required})" + (f"; {unknown} port tanpa data MTU" if unknown else "")
    if below_jumbo:
        return Check("port", "MTU-CORE", "warn", f"MTU cukup, tetapi sebagian di bawah {RECOMMENDED_CORE_MTU} (jumbo)", detail, below_jumbo)
    return Check("port", "MTU-CORE", "pass", "MTU di jalur cukup", detail)


# --------------------------------------------------------------------------- VLAN / VRF / RD / RT


def _same_port(service: m.Service, device_id: int | None, name: str | None) -> bool:
    if not device_id or not name:
        return False
    return (service.a_device_id == device_id and service.a_interface == name) or \
           (service.b_device_id == device_id and service.b_interface == name)


def _vlans(ctx: _Context) -> list[Check]:
    d = ctx.design
    checks: list[Check] = []
    if d.service_type in ("l2vpn", "iplc") and d.vlan_id is None:
        checks.append(Check("vlan", "VLAN-SET", "fail", "VLAN belum ditentukan", f"Layanan {d.service_type} butuh VLAN."))
    elif d.vlan_id == 1:
        checks.append(Check("vlan", "VLAN-SET", "warn", "VLAN 1 (default) dipakai", "Hindari VLAN default untuk layanan pelanggan."))
    clashes = []
    for side, device_id, name in (("A", d.a_device_id, d.a_interface), ("B", d.b_device_id, d.b_interface)):
        for other in ctx.other_services:
            if not _same_port(other, device_id, name):
                continue
            if other.vlan_id is None or d.vlan_id is None:
                clashes.append(f"{side}: {ctx.hostname(device_id)} {name} dipakai {other.circuit_id} tanpa VLAN (port penuh)")
            elif other.vlan_id == d.vlan_id:
                clashes.append(f"{side}: VLAN {d.vlan_id} di {ctx.hostname(device_id)} {name} sudah dipakai {other.circuit_id}")
    if clashes:
        checks.append(Check("vlan", "VLAN-CONFLICT", "fail", "Bentrok VLAN/port dengan layanan lain", "", clashes))
    elif d.vlan_id is not None or d.a_interface or d.b_interface:
        checks.append(Check("vlan", "VLAN-CONFLICT", "pass", "Tidak ada bentrok VLAN/port"))
    if d.service_type == "l3vpn":
        checks.extend(_vrf(ctx))
    return checks


def _vrf(ctx: _Context) -> list[Check]:
    d = ctx.design
    checks = []
    missing = [label for label, value in (("VRF", d.vrf_name), ("RD", d.route_distinguisher), ("RT", d.route_target)) if not value]
    if missing:
        checks.append(Check("vlan", "VRF-SET", "fail", f"L3VPN belum lengkap: {', '.join(missing)} kosong"))
    bad = [f"{label} '{value}'" for label, value in (("RD", d.route_distinguisher), ("RT", d.route_target))
           if value and not all(_RD_RT.match(v.strip()) for v in value.split(","))]
    if bad:
        checks.append(Check("vlan", "VRF-FORMAT", "fail", "Format RD/RT tidak valid", "Gunakan ASN:NN atau IPv4:NN", bad))
    if d.route_distinguisher:
        reuse = [f"{o.circuit_id} (VRF {o.vrf_name})" for o in ctx.other_services
                 if o.route_distinguisher == d.route_distinguisher and o.vrf_name != d.vrf_name]
        checks.append(Check("vlan", "VRF-RD", "fail", f"RD {d.route_distinguisher} sudah dipakai VRF lain", "", reuse) if reuse
                      else Check("vlan", "VRF-RD", "pass", f"RD {d.route_distinguisher} unik"))
    if d.route_target:
        mine = {v.strip() for v in d.route_target.split(",")}
        reuse = [f"{o.circuit_id} (VRF {o.vrf_name})" for o in ctx.other_services if o.route_target and o.vrf_name != d.vrf_name
                 and mine & {v.strip() for v in o.route_target.split(",")}]
        if reuse:
            checks.append(Check("vlan", "VRF-RT", "warn", "RT juga dipakai VRF lain",
                                "Wajar untuk hub-spoke/extranet; pastikan memang disengaja.", reuse))
    return checks


# --------------------------------------------------------------------------- kesiapan perangkat


def _devices(ctx: _Context) -> list[Check]:
    d = ctx.design
    on_path = ctx.path_devices(d.primary_link_ids + d.backup_link_ids)
    for end in (d.a_device_id, d.b_device_id):
        if end and end not in on_path:
            on_path.append(end)
    if not on_path:
        return []
    inactive, no_loopback, no_isis, no_srv6, old_sw, unknown_sw = [], [], [], [], [], []
    for device_id in on_path:
        device = ctx.devices.get(device_id)
        if device is None:
            continue
        if device.status != "active":
            inactive.append(f"{device.hostname} ({device.status})")
        if not device.loopback_ipv4:
            no_loopback.append(device.hostname)
        if not device.isis_net and device.role not in ("olt", "access"):
            no_isis.append(device.hostname)
        if not device.srv6_locator and device.role not in ("olt", "access"):
            no_srv6.append(device.hostname)
        sw = ctx.software.get(device.software_id) if device.software_id else None
        if sw is None or sw.vendor_id not in ctx.vendors_with_recommendation:
            unknown_sw.append(device.hostname)  # no version, or the vendor has no recommended version defined yet
        elif not sw.is_recommended:
            old_sw.append(f"{device.hostname} ({sw.version})")
    total = len(on_path)
    checks = [
        Check("perangkat", "DEV-STATUS", "fail" if inactive else "pass",
              "Perangkat tidak aktif di jalur" if inactive else f"Semua {total} perangkat di jalur aktif", "", inactive),
        Check("perangkat", "DEV-LOOPBACK", "warn" if no_loopback else "pass",
              f"{len(no_loopback)} perangkat tanpa loopback" if no_loopback else "Loopback tersedia di semua perangkat",
              "Loopback dibutuhkan sebagai router-id dan endpoint LDP/SR." if no_loopback else "", no_loopback),
        Check("perangkat", "DEV-ISIS", "warn" if no_isis else "pass",
              f"{len(no_isis)} router tanpa IS-IS NET" if no_isis else "IS-IS NET tersedia di semua router", "", no_isis),
    ]
    if no_srv6:
        checks.append(Check("perangkat", "DEV-SRV6", "info", f"{len(no_srv6)} router belum punya SRv6 locator",
                            "Informasi untuk migrasi SRv6; tidak menghalangi layanan MPLS.", no_srv6))
    if old_sw:
        checks.append(Check("perangkat", "DEV-SW", "warn", "Software tidak termasuk versi rekomendasi", "", old_sw))
    elif unknown_sw:
        checks.append(Check("perangkat", "DEV-SW", "info", f"Versi software {len(unknown_sw)} perangkat belum bisa dinilai",
                            "Versi tidak diketahui, atau vendornya belum punya versi rekomendasi (Inventory → Software).",
                            unknown_sw))
    else:
        checks.append(Check("perangkat", "DEV-SW", "pass", "Software semua perangkat versi rekomendasi"))
    return checks


# --------------------------------------------------------------------------- entry points


def validate(db: Session, design: ServiceDesign) -> dict[str, Any]:
    ctx = _Context(db, design)
    checks = _capacity(ctx) + _ports(ctx) + _vlans(ctx) + _devices(ctx)
    counts = {s: sum(1 for c in checks if c.status == s) for s in ("fail", "warn", "pass", "info")}
    overall = "fail" if counts["fail"] else "warn" if counts["warn"] else "pass"
    return {
        "status": overall,
        "counts": counts,
        "groups": [{"group": g, "label": label, "status": _worst([c for c in checks if c.group == g]),
                    "checks": [c.as_dict() for c in checks if c.group == g]} for g, label in GROUPS.items()],
    }


def _worst(checks: list[Check]) -> str:
    for status in ("fail", "warn", "pass"):
        if any(c.status == status for c in checks):
            return status
    return "info"


def design_for_service(db: Session, service: m.Service) -> ServiceDesign:
    """A stored service with its engineered paths (bandwidth already reserved when the path holds it)."""
    paths = {p.role: p for p in db.scalars(select(m.ServicePath).where(
        m.ServicePath.service_id == service.id, or_(m.ServicePath.released.is_(False), m.ServicePath.released.is_(None))))}
    primary, backup = paths.get("primary"), paths.get("backup")
    return ServiceDesign(
        service_type=service.service_type, bandwidth_mbps=service.bandwidth_mbps, protection=service.protection,
        a_device_id=service.a_device_id, a_interface=service.a_interface, b_device_id=service.b_device_id,
        b_interface=service.b_interface, vlan_id=service.vlan_id, vrf_name=service.vrf_name,
        route_distinguisher=service.route_distinguisher, route_target=service.route_target,
        primary_link_ids=list(primary.link_ids) if primary else [], backup_link_ids=list(backup.link_ids) if backup else [],
        service_id=service.id, already_reserved=bool(primary and primary.reserved_gbps),
    )
