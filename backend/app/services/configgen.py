"""Service configuration per endpoint (Phase 7): Huawei VRP, ZTE, Cisco IOS-XR, FiberHome.

Templates follow the service configs found in this network's RANCID backups (2026-11), not generic vendor
examples:
- l2vpn / iplc: LDP pseudowire to the other PE loopback. Huawei, ZTE and FiberHome build even point-to-point
  services as a VPLS instance with one peer (Huawei `vsi … pwsignal ldp`, ZTE `vpls`, FiberHome `vfi`); IOS-XR
  uses `xconnect group … p2p` with the `MPLS-CONTROL-WORD` pw-class. The VC id is the VLAN, as on the ZTE metro,
  unless given; the L2 MTU is the 2000 used by the metro VPLS instances. A VPLS request may list several VLANs:
  each gets its own sub-interface and instance (OXY_1480 … OXY_1485 in the backups). On ZTE the pw names are
  allocated from the device's RANCID running config: the lowest pwN not yet declared or referenced.
- l3vpn: VRF with RD/RT from the service, sub-interface in the VRF, connected routes into BGP. The VRF block is
  only written (and removed on rollback) when no other live service already uses that VRF on the device.
- internet: sub-interface with a public address in the global table.
Policers at the service bandwidth are optional (off by default) and use each vendor's form seen in the network
(Huawei `qos car`, ZTE `qos` → `rate-limit`, IOS-XR policy-map). FiberHome metro boxes carry only L2 VPLS and no
policers in the backups, so L3 services and policing are not generated for them.

The output is pasted as is: no comment lines. Views are left with the vendor's exit command (VRP `quit`,
ZTE `$`, FiberHome `exit`, IOS-XR `root`); remarks go to `warnings`, shown beside the config instead.
Customer-facing addresses and the BGP ASN are not in the inventory: they come from the request, else the ASN is
read from the RD and addresses stay placeholders.
"""

import ipaddress
import re
from dataclasses import dataclass, field
from collections.abc import Callable
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models as m
from . import running_config
from .l2vpn_peers import pw_usage
from .vendors import DIALECT_LABELS, dialect_for

VC_ID_BASE = 100000  # services without a VLAN: 100000 + service id
MAX_VLANS = 64
L2_MTU = 2000
SYNTAX_WARNING = "Template mengikuti config layanan di backup RANCID; tetap cocokkan dengan versi software perangkat."


@dataclass
class Addressing:
    """Optional request inputs: PE-side addresses (CIDR), a fixed VC id, the BGP ASN and whether to police."""

    a_ip: str | None = None
    b_ip: str | None = None
    vc_id: int | None = None
    bgp_asn: int | None = None
    policer: bool = False
    vlans: list[int] | None = None  # VPLS batch: one sub-interface + instance per VLAN instead of the service VLAN
    vc_ids: dict[int, int] | None = None  # per-VLAN VC id, for VLANs whose default (VC id = VLAN) clashes
    # L2VPN neighbour written on site A / B (the far PE's loopback) when not the inventory loopback: an existing
    # neighbour picked from the running config, or a new destination typed in.
    a_peer: str | None = None
    b_peer: str | None = None


@dataclass
class _Ctx:
    service: m.Service
    customer: str
    name: str  # VSI / VPLS / VFI / p2p instance name, e.g. PT-CONTOH_1201
    group: str  # IOS-XR xconnect group, e.g. xg_PT-CONTOH
    port: str
    sub: str
    vlan: int | None
    peer_loopback: str
    vc_id: int
    kbps: int
    policer: bool
    ip: str | None
    mask: str | None
    vrf: str
    rd: str
    rt: list[str]
    asn: str
    vrf_exists: bool
    warnings: list[str] = field(default_factory=list)
    vc_conflicts: list[str] = field(default_factory=list)  # why this VC id cannot be used as is
    pw: str | None = None  # ZTE pw name allocated from the running config
    first: bool = True  # first VLAN of a batch: carries the vendor's entry command and shared blocks

    @property
    def mbps(self) -> int:
        return max(self.kbps // 1000, 1)

    @property
    def description(self) -> str:
        return f"{self.service.circuit_id} {self.customer}".strip()[:80]


class AddressError(ValueError):
    pass


def parse_cidr(value: str | None) -> tuple[str, str, int] | None:
    if not value:
        return None
    try:
        iface = ipaddress.IPv4Interface(value.strip())
    except ValueError as exc:
        raise AddressError(f"Alamat tidak valid: {value} (contoh 10.10.10.1/30)") from exc
    if not 8 <= iface.network.prefixlen <= 31 or "/" not in value:
        raise AddressError(f"Alamat {value} harus dengan prefix /8–/31")
    return str(iface.ip), str(iface.netmask), iface.network.prefixlen


def parse_peer(value: str | None) -> str | None:
    """L2VPN neighbour: a plain IPv4 address (the far PE's loopback), no prefix."""
    if not value or not value.strip():
        return None
    try:
        return str(ipaddress.IPv4Address(value.strip()))
    except ValueError as exc:
        raise AddressError(f"Neighbour tidak valid: {value} (contoh 10.0.0.1)") from exc


def parse_vlans(value: str | None) -> list[int] | None:
    """"1480-1485,1489" -> [1480, …, 1485, 1489] (sorted, unique, at most MAX_VLANS)."""
    if not value or not value.strip():
        return None
    vlans: set[int] = set()
    for part in re.split(r"[,\s]+", value.strip()):
        match = re.fullmatch(r"(\d{1,4})(?:-(\d{1,4}))?", part)
        if not match:
            raise AddressError(f"Daftar VLAN tidak valid: {part} (contoh 1480-1485,1489)")
        low, high = int(match.group(1)), int(match.group(2) or match.group(1))
        if not 1 <= low <= high <= 4094:
            raise AddressError(f"Rentang VLAN {part} di luar 1–4094")
        vlans.update(range(low, high + 1))
        if len(vlans) > MAX_VLANS:
            raise AddressError(f"Maksimal {MAX_VLANS} VLAN per batch")
    return sorted(vlans)


def parse_vc_ids(value: str | None) -> dict[int, int] | None:
    """"1480:9001,1481:9002" -> {1480: 9001, 1481: 9002}: VLAN -> VC id overrides."""
    if not value or not value.strip():
        return None
    vc_ids: dict[int, int] = {}
    for part in re.split(r"[,\s]+", value.strip()):
        match = re.fullmatch(r"(\d{1,4}):(\d{1,10})", part)
        if not match:
            raise AddressError(f"VC id per VLAN tidak valid: {part} (contoh 1480:9001)")
        vlan, vc_id = int(match.group(1)), int(match.group(2))
        if not 1 <= vlan <= 4094 or not 1 <= vc_id <= 4294967295:
            raise AddressError(f"VC id per VLAN {part} di luar rentang")
        vc_ids[vlan] = vc_id
    return vc_ids


def check_vc_ids(vlans: list[int] | None, vc_ids: dict[int, int] | None) -> None:
    """Overrides must name VLANs of the batch, and no two VLANs may end up on the same VC id."""
    if not vc_ids:
        return
    if not vlans:
        raise AddressError("VC id per VLAN hanya untuk batch VLAN; untuk VLAN layanan isi VC id.")
    unknown = sorted(set(vc_ids) - set(vlans))
    if unknown:
        raise AddressError(f"VC id diisi untuk VLAN yang tidak ada di daftar: {', '.join(map(str, unknown))}")
    effective = [vc_ids.get(vlan, vlan) for vlan in vlans]
    duplicates = sorted({vc for vc in effective if effective.count(vc) > 1})
    if duplicates:
        raise AddressError(f"VC id dipakai lebih dari satu VLAN di batch ini: {', '.join(map(str, duplicates))}")


ConfigSource = Callable[[int], str]  # device id -> running config text (raises RunningConfigError)


def generate(db: Session, service: m.Service, addressing: Addressing | None = None,
             config_source: ConfigSource | None = None) -> dict[str, Any]:
    """{"endpoints": {"A": ..., "B": ...}}; each endpoint has config, rollback, warnings and notes.

    `config_source` reads a device's running config (ZTE pw allocation); RANCID gitweb unless given."""
    addressing = addressing or Addressing()
    config_source = config_source or (lambda device_id: running_config.fetch(db, device_id))
    customer = db.get(m.Customer, service.customer_id) if service.customer_id else None
    ends = {
        "A": (service.a_device_id, service.a_interface, service.b_device_id, addressing.a_ip, addressing.a_peer),
        "B": (service.b_device_id, service.b_interface, service.a_device_id, addressing.b_ip, addressing.b_peer),
    }
    dialects = {label: _dialect(db, device_id) for label, (device_id, *_rest) in ends.items()}
    vlans = _vlans(service, addressing)
    result: dict[str, Any] = {"service_id": service.id, "circuit_id": service.circuit_id,
                              "service_type": service.service_type, "vlans": vlans,
                              "vc_id": vc_id_for(service, addressing, vlans[0] if vlans else None),
                              "policer": addressing.policer, "endpoints": {}}
    for label, (device_id, port, peer_id, ip, peer_ip) in ends.items():
        peer_dialect = dialects["B" if label == "A" else "A"]
        result["endpoints"][label] = _endpoint(db, service, customer, label, device_id, port, peer_id, ip, peer_ip,
                                               addressing, peer_dialect, vlans, config_source)
    return result


def _vlans(service: m.Service, addressing: Addressing) -> list[int]:
    if addressing.vlans and _kind(service.service_type) == "l2vpn":
        return addressing.vlans
    return [service.vlan_id] if service.vlan_id else []


def vc_id_for(service: m.Service, addressing: Addressing, vlan: int | None = None) -> int:
    """VC id = VLAN unless overridden per VLAN; a single VC id only applies to a single-VLAN service."""
    vlan = vlan if vlan is not None else service.vlan_id
    if addressing.vc_ids and vlan in addressing.vc_ids:
        return addressing.vc_ids[vlan]
    single = not addressing.vlans or len(addressing.vlans) == 1
    return (addressing.vc_id if single else None) or vlan or VC_ID_BASE + service.id


def dialect_of(db: Session, device_id: int | None) -> str | None:
    """Generator dialect of a device (vrp, zte, iosxr, fiberhome), None when unsupported or unknown."""
    return _dialect(db, device_id)


def _dialect(db: Session, device_id: int | None) -> str | None:
    device = db.get(m.Device, device_id) if device_id else None
    if device is None:
        return None
    vendor = db.get(m.Vendor, device.vendor_id) if device.vendor_id else None
    return dialect_for(vendor.name if vendor else None, vendor.os_family if vendor else None, device.hostname)


def _endpoint(db: Session, service: m.Service, customer: m.Customer | None, label: str, device_id: int | None,
              port: str | None, peer_id: int | None, ip: str | None, peer_ip: str | None, addressing: Addressing,
              peer_dialect: str | None, vlans: list[int], config_source: ConfigSource) -> dict[str, Any]:
    device = db.get(m.Device, device_id) if device_id else None
    out: dict[str, Any] = {"label": label, "device_id": device_id, "device": device.hostname if device else None,
                           "dialect": None, "dialect_label": "Vendor tidak dikenali", "config": [], "rollback": [],
                           "commit": [], "warnings": [], "notes": []}
    if device is None or not port:
        out["warnings"].append(f"Site {label} belum punya perangkat dan port akses.")
        return out
    dialect = _dialect(db, device_id)
    out["dialect"] = dialect
    if dialect is None:
        out["warnings"].append("Vendor/OS perangkat tidak didukung generator (VRP, ZTE, IOS-XR, FiberHome); tulis manual.")
        return out
    out["dialect_label"] = DIALECT_LABELS[dialect]
    kind = _kind(service.service_type)
    template = TEMPLATES[dialect].get(kind)
    if template is None:
        out["warnings"].append(f"{service.service_type.upper()} tidak dibuat untuk {DIALECT_LABELS[dialect]}: perangkat "
                               "jenis ini di jaringan hanya membawa L2 (VPLS), tidak ada contoh konfigurasinya.")
        return out
    contexts = [_context(db, service, customer, device, port, peer_id, ip, peer_ip, addressing, vlan, first=i == 0)
                for i, vlan in enumerate(vlans or [None])]
    warnings: list[str] = []
    if kind == "l2vpn" and peer_dialect and (dialect == "fiberhome") != (peer_dialect == "fiberhome"):
        warnings.append("FiberHome memakai pseudowire tanpa control-word (encapsulation raw), vendor lain dengan "
                        "control-word: samakan pengaturan control-word di kedua ujung.")
    if addressing.vc_id and len(contexts) > 1:
        warnings.append("VC id manual diabaikan untuk batch beberapa VLAN: isi VC id per VLAN.")
    if kind == "l2vpn":
        _check_running_config(contexts, config_source, device.id, dialect, out["notes"], warnings)
    config: list[str] = []
    rollback: list[str] = []
    for ctx in contexts:
        part_config, part_rollback = template(ctx)
        # The entry command (system-view / configure …) opens the script once for the whole batch.
        config += part_config if ctx.first else part_config[1:]
        rollback += part_rollback if ctx.first else part_rollback[1:]
        warnings += ctx.warnings
    unique = list(dict.fromkeys(warnings + [SYNTAX_WARNING]))
    out.update(config=config, rollback=rollback, commit=COMMIT[dialect], warnings=unique)
    if kind == "l2vpn":
        out["vc_ids"] = [{"vlan": ctx.vlan, "vc_id": ctx.vc_id, "pw": ctx.pw, "conflicts": ctx.vc_conflicts}
                         for ctx in contexts]
    return out


_ZTE_PW = re.compile(r"^pw (\S+)\s*$", re.M)
_ZTE_PW_REF = re.compile(r"^\s+pseudo-wire (\S+)", re.M)
_ZTE_NEIGHBOUR = re.compile(r"^\s+neighbour (\S+) vcid (\d+)", re.M)


def zte_pw_usage(text: str) -> tuple[set[str], set[tuple[str, int]]]:
    """pw names already declared or referenced, and (neighbour, vcid) pairs in use, from a ZXROS running config."""
    names = set(_ZTE_PW.findall(text)) | set(_ZTE_PW_REF.findall(text))
    return names, {(peer, int(vcid)) for peer, vcid in _ZTE_NEIGHBOUR.findall(text)}


def allocate_pw(used: set[str], count: int) -> list[str]:
    """Lowest free pwN names, following the device's own pw1, pw2 … numbering."""
    names: list[str] = []
    number = 1
    while len(names) < count:
        if f"pw{number}" not in used:
            names.append(f"pw{number}")
        number += 1
    return names


def _check_running_config(contexts: list[_Ctx], config_source: ConfigSource, device_id: int, dialect: str,
                          notes: list[str], warnings: list[str]) -> None:
    """PW / VC ids already used towards the same neighbour (every dialect), and ZTE pw names from the RANCID config."""
    try:
        text = config_source(device_id)
    except running_config.RunningConfigError as exc:
        if dialect == "zte":
            warnings.append(f"{exc} pw dinamai pw<VC id>; cek manual bahwa nama itu belum dipakai.")
        else:
            notes.append(f"PW id belum dicek ke config perangkat: {exc}")
        return
    in_use = set(pw_usage(text, dialect))
    for ctx in contexts:
        if (ctx.peer_loopback, ctx.vc_id) in in_use:
            ctx.vc_conflicts.append(f"sudah ada di config RANCID (neighbour {ctx.peer_loopback})")
            warnings.append(f"vcid {ctx.vc_id} ke neighbour {ctx.peer_loopback} sudah ada di config perangkat: "
                            "isi VC id lain.")
    if dialect == "zte":
        _allocate_zte_pw(contexts, text, notes, warnings)


def _allocate_zte_pw(contexts: list[_Ctx], text: str, notes: list[str], warnings: list[str]) -> None:
    used, _vcids = zte_pw_usage(text)
    for ctx, pw in zip(contexts, allocate_pw(used, len(contexts))):
        ctx.pw = pw
        if re.search(rf"^interface {re.escape(ctx.sub)}\s*$", text, re.M):
            warnings.append(f"Sub-interface {ctx.sub} sudah ada di config perangkat.")
    notes.append(f"{len(used)} pw sudah terpakai di config RANCID; dialokasikan: "
                 + ", ".join(f"VLAN {c.vlan or '-'} → {c.pw}" for c in contexts))


def _kind(service_type: str) -> str:
    return {"iplc": "l2vpn"}.get(service_type, service_type)


def _slug(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "-", text).strip("-").upper()


def instance_name(service: m.Service, customer: m.Customer | None, vlan: int | None = None) -> str:
    """L2 instance name in the network's style (OXY_1480, BBT_1490): customer + VLAN, else the circuit id."""
    vlan = vlan if vlan is not None else service.vlan_id
    slug = _slug(customer.name)[:20] if customer else ""
    tag = re.sub(r"[^A-Za-z0-9_-]+", "_", service.circuit_id)
    return f"{slug or tag}_{vlan}" if vlan else tag


def _vc_clash(db: Session, service: m.Service, vc_id: int) -> str | None:
    """VC id = VLAN must be unique between the same two PEs; another live service there with that VLAN clashes."""
    pair = {service.a_device_id, service.b_device_id}
    others = db.scalars(select(m.Service).where(
        m.Service.id != service.id, m.Service.vlan_id == vc_id, m.Service.status != "terminated",
        m.Service.service_type.in_(("l2vpn", "iplc"))))
    return next((o.circuit_id for o in others if {o.a_device_id, o.b_device_id} == pair), None)


def _context(db: Session, service: m.Service, customer: m.Customer | None, device: m.Device, port: str,
             peer_id: int | None, ip: str | None, peer_ip: str | None, addressing: Addressing, vlan: int | None,
             first: bool) -> _Ctx:
    warnings: list[str] = []
    sub = f"{port}.{vlan}" if vlan else port
    if not vlan:
        warnings.append("VLAN belum diisi: konfigurasi memakai port fisik tanpa sub-interface.")
    elif db.scalar(select(m.Interface.id).where(m.Interface.device_id == device.id, m.Interface.name == sub)):
        warnings.append(f"Sub-interface {sub} sudah ada di inventory: pastikan tidak dipakai layanan lain.")
    peer = db.get(m.Device, peer_id) if peer_id else None
    peer_loopback = peer_ip or (peer.loopback_ipv4 if peer else None)
    vc_id = vc_id_for(service, addressing, vlan)
    vc_conflicts: list[str] = []
    if _kind(service.service_type) == "l2vpn":
        if not peer_loopback:
            warnings.append("Loopback PE lawan belum diketahui: pilih neighbour atau isi <loopback-peer> secara manual.")
        clash = _vc_clash(db, service, vc_id)
        if clash:
            vc_conflicts.append(f"dipakai layanan {clash} antara dua PE yang sama")
            warnings.append(f"VC id {vc_id} juga dipakai layanan {clash} antara dua PE yang sama: isi VC id lain.")
    parsed = parse_cidr(ip)
    if service.service_type in ("l3vpn", "internet") and parsed is None:
        warnings.append("Alamat IP sisi PE belum diisi: ganti <ip> dan <mask>.")
    rts = [r.strip() for r in re.split(r"[,\s]+", service.route_target or "") if r.strip()]
    rd = service.route_distinguisher or ""
    asn = str(addressing.bgp_asn) if addressing.bgp_asn else ""
    if service.service_type == "l3vpn":
        if not (service.vrf_name and rd and rts):
            warnings.append("VRF/RD/RT belum lengkap: placeholder dipakai.")
        if not asn and re.fullmatch(r"\d+:\d+", rd):
            asn = rd.split(":")[0]
            # RDs here do not always carry the local AS (e.g. RD 10001:90003 on a box in AS 46038).
            warnings.append(f"ASN BGP {asn} diambil dari RD; isi ASN bila AS perangkat berbeda.")
        elif not asn:
            warnings.append("ASN BGP belum diketahui: isi ASN atau ganti <asn>.")
    vrf_exists = bool(service.vrf_name) and _vrf_on_device(db, service, device.id)
    if vrf_exists:
        warnings.append(f"VRF {service.vrf_name} sudah dipakai layanan lain di perangkat ini: blok VRF tidak dibuat ulang "
                        "dan tidak dihapus saat rollback.")
    customer_slug = _slug(customer.name)[:20] if customer else ""
    return _Ctx(
        service=service, customer=customer.name if customer else "",
        name=instance_name(service, customer, vlan), group=f"xg_{customer_slug or 'NADIEN'}",
        port=port, sub=sub, vlan=vlan,
        peer_loopback=peer_loopback or "<loopback-peer>",
        vc_id=vc_id, kbps=service.bandwidth_mbps * 1000, policer=addressing.policer,
        ip=parsed[0] if parsed else None, mask=parsed[1] if parsed else None,
        vrf=service.vrf_name or "<vrf>", rd=rd or "<rd>", rt=rts or ["<rt>"], asn=asn or "<asn>",
        vrf_exists=vrf_exists, warnings=warnings, vc_conflicts=vc_conflicts, first=first,
    )


def _vrf_on_device(db: Session, service: m.Service, device_id: int) -> bool:
    others = db.scalars(select(m.Service).where(
        m.Service.id != service.id, m.Service.vrf_name == service.vrf_name, m.Service.status != "terminated"))
    return any(device_id in (o.a_device_id, o.b_device_id) for o in others)


def _ip(ctx: _Ctx) -> str:
    return f"{ctx.ip} {ctx.mask}" if ctx.ip else "<ip> <mask>"


# --------------------------------------------------------------------------- Huawei VRP


def _vrp_car(ctx: _Ctx) -> list[str]:
    if not ctx.policer:
        return []
    cbs = ctx.kbps * 187  # cbs/cir ratio of the qos car lines in the backups
    return [f" qos car cir {ctx.kbps} cbs {cbs} green pass red discard inbound",
            f" qos car cir {ctx.kbps} cbs {cbs} green pass red discard outbound"]


def _vrp_sub(ctx: _Ctx) -> list[str]:
    lines = [f"interface {ctx.sub}"]
    if ctx.vlan:
        lines.append(f" vlan-type dot1q {ctx.vlan}")
    return lines + [f" description {ctx.description}", " statistic enable"]


def _vrp_l2vpn(ctx: _Ctx) -> tuple[list[str], list[str]]:
    config = ["system-view", f"vsi {ctx.name}", " pwsignal ldp", f"  vsi-id {ctx.vc_id}", "  control-word enable",
              f"  peer {ctx.peer_loopback}", "  quit", f" mtu {L2_MTU}", " encapsulation ethernet", " quit",
              *_vrp_sub(ctx), f" l2 binding vsi {ctx.name}", *_vrp_car(ctx), " quit"]
    return config, ["system-view", f"undo interface {ctx.sub}", f"undo vsi {ctx.name}"]


def _vrp_l3vpn(ctx: _Ctx) -> tuple[list[str], list[str]]:
    config = ["system-view"]
    if not ctx.vrf_exists:
        config += [f"ip vpn-instance {ctx.vrf}", " ipv4-family", f"  route-distinguisher {ctx.rd}",
                   "  apply-label per-instance", *[f"  vpn-target {rt} export-extcommunity" for rt in ctx.rt],
                   *[f"  vpn-target {rt} import-extcommunity" for rt in ctx.rt], "  quit", " quit"]
    config += [*_vrp_sub(ctx), f" ip binding vpn-instance {ctx.vrf}", f" ip address {_ip(ctx)}", *_vrp_car(ctx),
               " quit"]
    if not ctx.vrf_exists:
        config += [f"bgp {ctx.asn}", f" ipv4-family vpn-instance {ctx.vrf}", "  import-route direct", "  quit", " quit"]
    rollback = ["system-view", f"undo interface {ctx.sub}"]
    if not ctx.vrf_exists:
        rollback += [f"bgp {ctx.asn}", f" undo ipv4-family vpn-instance {ctx.vrf}", " quit", f"undo ip vpn-instance {ctx.vrf}"]
    return config, rollback


def _vrp_internet(ctx: _Ctx) -> tuple[list[str], list[str]]:
    config = ["system-view", *_vrp_sub(ctx), f" ip address {_ip(ctx)}", *_vrp_car(ctx), " quit"]
    return config, ["system-view", f"undo interface {ctx.sub}"]


# --------------------------------------------------------------------------- Cisco IOS-XR


def _xr_policy(ctx: _Ctx) -> list[str]:
    if not ctx.policer or not ctx.first:  # one policy-map serves the whole batch
        return []
    return [f"policy-map NADIEN-{ctx.mbps}M", " class class-default", f"  police rate {ctx.mbps} mbps", "  exit",
            " end-policy-map"]


def _xr_service_policy(ctx: _Ctx) -> list[str]:
    return [f" service-policy input NADIEN-{ctx.mbps}M"] if ctx.policer else []


def _xr_l2vpn(ctx: _Ctx) -> tuple[list[str], list[str]]:
    config = ["configure", *_xr_policy(ctx), f"interface {ctx.sub} l2transport", f" description {ctx.description}"]
    if ctx.vlan:
        config += [f" encapsulation dot1q {ctx.vlan}", " rewrite ingress tag pop 1 symmetric"]
    config += [*_xr_service_policy(ctx), "root", "l2vpn", f" xconnect group {ctx.group}", f"  p2p {ctx.name}",
               f"   interface {ctx.sub}", f"   neighbor ipv4 {ctx.peer_loopback} pw-id {ctx.vc_id}",
               "    pw-class MPLS-CONTROL-WORD", "root"]
    ctx.warnings.append("pw-class MPLS-CONTROL-WORD harus sudah ada di perangkat (dipakai di metro Cisco).")
    rollback = ["configure", f"no l2vpn xconnect group {ctx.group} p2p {ctx.name}", f"no interface {ctx.sub}"]
    return config, rollback


def _xr_sub_l3(ctx: _Ctx, vrf: bool) -> list[str]:
    lines = [f"interface {ctx.sub}", f" description {ctx.description}"]
    if vrf:
        lines.append(f" vrf {ctx.vrf}")
    lines.append(f" ipv4 address {_ip(ctx)}")
    if ctx.vlan:
        lines.append(f" encapsulation dot1q {ctx.vlan}")
    return lines + _xr_service_policy(ctx) + ["root"]


def _xr_l3vpn(ctx: _Ctx) -> tuple[list[str], list[str]]:
    config = ["configure", *_xr_policy(ctx)]
    if not ctx.vrf_exists:
        config += [f"vrf {ctx.vrf}", " address-family ipv4 unicast", *[f"  import route-target {rt}" for rt in ctx.rt],
                   *[f"  export route-target {rt}" for rt in ctx.rt], "root"]
    config += _xr_sub_l3(ctx, vrf=True)
    if not ctx.vrf_exists:
        config += [f"router bgp {ctx.asn}", f" vrf {ctx.vrf}", f"  rd {ctx.rd}", "  address-family ipv4 unicast",
                   "   redistribute connected", "root"]
    rollback = ["configure", f"no interface {ctx.sub}"]
    if not ctx.vrf_exists:
        rollback += [f"router bgp {ctx.asn}", f" no vrf {ctx.vrf}", "root", f"no vrf {ctx.vrf}"]
    return config, rollback


def _xr_internet(ctx: _Ctx) -> tuple[list[str], list[str]]:
    return ["configure", *_xr_policy(ctx), *_xr_sub_l3(ctx, vrf=False)], ["configure", f"no interface {ctx.sub}"]


# --------------------------------------------------------------------------- ZTE (ZXROS: features configured per section)


def _zte_sections(ctx: _Ctx, interface_lines: list[str]) -> list[str]:
    """Interface block, then its VLAN under vlan-configuration, as in the ZXROS backups."""
    lines = [f"interface {ctx.sub}", f"  description {ctx.description}", *interface_lines, "$"]
    if ctx.vlan:
        lines += ["vlan-configuration", f"  interface {ctx.sub}", f"    encapsulation-dot1q {ctx.vlan}", "  $", "$"]
    return lines


def _zte_qos(ctx: _Ctx) -> list[str]:
    if not ctx.policer:
        return []
    cbs = ctx.kbps * 125 // 10  # cbs/cir ratio of the rate-limit lines in the backups
    return ["qos", f"  interface {ctx.sub}",
            f"    rate-limit input cir {ctx.kbps} kbps cbs {cbs} pir {ctx.kbps} kbps",
            f"    rate-limit output cir {ctx.kbps} kbps cbs {cbs} pir {ctx.kbps} kbps", "  $", "$"]


def _zte_l2vpn(ctx: _Ctx) -> tuple[list[str], list[str]]:
    pw = ctx.pw or f"pw{ctx.vc_id}"
    config = ["configure terminal", *_zte_sections(ctx, []), f"pw {pw}", f"vpls {ctx.name}", f"  mtu {L2_MTU}",
              f"  access-point {ctx.sub}", "    access-params ethernet", "    $", "  $", f"  pseudo-wire {pw}",
              f"    neighbour {ctx.peer_loopback} vcid {ctx.vc_id} cw-preferred", "    $", "  $", "$", *_zte_qos(ctx)]
    rollback = ["configure terminal", f"no vpls {ctx.name}", f"no pw {pw}", f"no interface {ctx.sub}"]
    return config, rollback


def _zte_l3vpn(ctx: _Ctx) -> tuple[list[str], list[str]]:
    config = ["configure terminal"]
    if not ctx.vrf_exists:
        config += [f"ip vrf {ctx.vrf}", f"  rd {ctx.rd}", "  address-family ipv4",
                   *[f"    route-target import {rt}" for rt in ctx.rt], *[f"    route-target export {rt}" for rt in ctx.rt],
                   "  $", "$"]
    config += _zte_sections(ctx, [f"  ip vrf forwarding {ctx.vrf}", f"  ip address {_ip(ctx)}"])
    if not ctx.vrf_exists:
        config += [f"router bgp {ctx.asn}", f"  address-family ipv4 vrf {ctx.vrf}", "    redistribute connected", "  $", "$"]
    config += _zte_qos(ctx)
    rollback = ["configure terminal", f"no interface {ctx.sub}"]
    if not ctx.vrf_exists:
        rollback += [f"router bgp {ctx.asn}", f"  no address-family ipv4 vrf {ctx.vrf}", "$", f"no ip vrf {ctx.vrf}"]
    return config, rollback


def _zte_internet(ctx: _Ctx) -> tuple[list[str], list[str]]:
    config = ["configure terminal", *_zte_sections(ctx, [f"  ip address {_ip(ctx)}"]), *_zte_qos(ctx)]
    return config, ["configure terminal", f"no interface {ctx.sub}"]


# --------------------------------------------------------------------------- FiberHome (L2 VPLS only)


def fiberhome_name(port: str) -> str:
    """Inventory keeps "eth-10gi0/1/0/3" / "lag11.1495"; the CLI writes "eth-10gi 0/1/0/3" / "lag 11.1495"."""
    match = re.fullmatch(r"(.*?[A-Za-z])\s*(\d+(?:/\d+)*(?:\.\d+)?)", port)
    return f"{match.group(1)} {match.group(2)}" if match else port


def _fh_l2vpn(ctx: _Ctx) -> tuple[list[str], list[str]]:
    sub = fiberhome_name(ctx.sub)
    config = ["configure terminal", f"interface {sub}", " l2transport", f" description {ctx.description}",
              f" mtu {L2_MTU}"]
    if ctx.vlan:
        config += [" encapsulation dot1q", f" vlan-type dot1q {ctx.vlan}"]
    config += [" exit", f"vfi {ctx.name}", f" vsi-id {ctx.vc_id}", f" mtu {L2_MTU}", f" interface {sub}", "  exit",
               f" peer {ctx.peer_loopback} encapsulation raw", "  exit", " exit"]
    if ctx.policer:
        ctx.warnings.append("Policer tidak dibuat di FiberHome: backup FiberHome metro tidak memuat contoh QoS; batasi "
                            "bandwidth di ujung lain atau sesuai standar FiberHome.")
    return config, ["configure terminal", f"no vfi {ctx.name}", f"no interface {sub}"]


TEMPLATES = {
    "vrp": {"l2vpn": _vrp_l2vpn, "l3vpn": _vrp_l3vpn, "internet": _vrp_internet},
    "iosxr": {"l2vpn": _xr_l2vpn, "l3vpn": _xr_l3vpn, "internet": _xr_internet},
    "zte": {"l2vpn": _zte_l2vpn, "l3vpn": _zte_l3vpn, "internet": _zte_internet},
    "fiberhome": {"l2vpn": _fh_l2vpn},
}

# Saving is a commit on every platform here; no write/save.
COMMIT: dict[str, list[str]] = {"vrp": ["commit", "return"], "zte": ["commit", "end"], "fiberhome": ["commit", "end"],
                                "iosxr": ["commit", "end"]}


def as_text(result: dict[str, Any], side: Literal["A", "B"], part: Literal["config", "rollback"]) -> str:
    """One site's config or rollback, ready to paste: commands and the commit, nothing else."""
    endpoint = result["endpoints"][side]
    lines = endpoint[part] + endpoint["commit"] if endpoint[part] else []
    return "\n".join(lines) + ("\n" if lines else "")
