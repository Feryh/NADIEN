"""Method of Procedure (Phase 6) for one service: structured content, rendered to PDF by mop_pdf.

Sections follow a usual change document: purpose, service summary, paths, validation result (Phase 5),
risk & impact, preparation, pre-check, execution, verification, rollback, contacts. CLI commands are
written per device in its own dialect (Huawei VRP, ZTE, Cisco IOS-XR, FiberHome). Execution and
rollback take the service configuration from the config generator (Phase 7) when one is given.
"""

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models as m
from .validation import GROUPS, design_for_service, validate
from .vendors import DIALECT_LABELS, dialect_for

# Command templates per dialect. Placeholders: {port} {sub} (port.vlan) {vlan} {vrf} {lo} {peer_lo} {circuit},
# {circuit_tag} (circuit id safe for file names and commit labels).
COMMANDS: dict[str, dict[str, list[str]]] = {
    "vrp": {
        "backup": ["display current-configuration", "save"],
        "port": ["display interface {port}", "display current-configuration interface {port}"],
        "sub": ["display current-configuration interface {sub}"],
        "core": ["display isis peer", "display mpls ldp session"],
        "vrf": ["display ip vpn-instance {vrf}"],
        "ping": ["ping -a {lo} {peer_lo}"],
        "commit": ["commit", "save"],
        "verify_l2": ["display interface {sub}", "display mpls l2vc interface {sub}"],
        "verify_l3": ["display interface {sub}", "display ip routing-table vpn-instance {vrf}",
                      "display bgp vpnv4 vpn-instance {vrf} peer"],
        "verify_inet": ["display interface {sub}", "display ip routing-table | include {sub}"],
        "rollback": ["display configuration commit list", "rollback configuration to commit-id <commit-id sebelum perubahan>"],
    },
    "zte": {
        "backup": ["show running-config", "write"],
        "port": ["show interface {port}", "show running-config interface {port}"],
        "sub": ["show running-config interface {sub}"],
        "core": ["show isis adjacency", "show mpls ldp neighbor"],
        "vrf": ["show ip vrf {vrf}"],
        "ping": ["ping {peer_lo} source {lo}"],
        "commit": ["write"],
        "verify_l2": ["show interface {sub}", "show mpls l2transport vc detail"],
        "verify_l3": ["show interface {sub}", "show ip route vrf {vrf}", "show bgp vpnv4 unicast vrf {vrf} summary"],
        "verify_inet": ["show interface {sub}", "show ip route | include {sub}"],
        "rollback": ["(jalankan konfigurasi rollback di bawah)", "write"],
    },
    "iosxr": {
        "backup": ["show running-config", "copy running-config harddisk:/backup-{circuit_tag}.cfg"],
        "port": ["show interfaces {port}", "show running-config interface {port}"],
        "sub": ["show running-config interface {sub}"],
        "core": ["show isis adjacency", "show mpls ldp neighbor brief"],
        "vrf": ["show vrf {vrf}"],
        "ping": ["ping {peer_lo} source {lo}"],
        "commit": ["commit label {circuit_tag}", "show configuration commit list 1"],
        "verify_l2": ["show interfaces {sub}", "show l2vpn xconnect interface {sub} detail"],
        "verify_l3": ["show interfaces {sub}", "show route vrf {vrf}", "show bgp vrf {vrf} summary"],
        "verify_inet": ["show interfaces {sub}", "show route | include {sub}"],
        "rollback": ["show configuration commit list", "rollback configuration to {circuit_tag}  ! label commit layanan ini",
                     "(atau) rollback configuration last 1"],
    },
    "fiberhome": {
        "backup": ["show running-config", "write file"],
        "port": ["show interface {port}", "show running-config interface {port}"],
        "sub": ["show running-config interface {sub}"],
        "core": ["show isis neighbor", "show mpls ldp session"],
        "vrf": ["show ip vrf {vrf}"],
        "ping": ["ping {peer_lo} source {lo}"],
        "commit": ["write file"],
        "verify_l2": ["show interface {sub}", "show mpls l2vc"],
        "verify_l3": ["show interface {sub}", "show ip route vrf {vrf}"],
        "verify_inet": ["show interface {sub}", "show ip route"],
        "rollback": ["(jalankan konfigurasi rollback di bawah)", "write file"],
    },
}
NOTE_SYNTAX = ("Perintah ditulis dengan sintaks umum per vendor; periksa kesesuaian dengan versi software perangkat "
               "sebelum eksekusi.")


@dataclass
class MopMeta:
    number: str = ""
    change_ticket: str = ""
    window_start: str = ""
    window_end: str = ""
    executor: str = ""
    reviewer: str = ""
    approver: str = ""


@dataclass
class Step:
    device: str
    dialect: str | None
    title: str
    commands: list[str] = field(default_factory=list)
    note: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"device": self.device, "dialect": self.dialect, "dialect_label": DIALECT_LABELS.get(self.dialect or "", "Vendor tidak dikenali"),
                "title": self.title, "commands": self.commands, "note": self.note}


@dataclass
class Section:
    title: str
    paragraphs: list[str] = field(default_factory=list)
    table: dict[str, Any] | None = None  # {"headers": [...], "rows": [[...]]}
    tables: list[dict[str, Any]] = field(default_factory=list)
    steps: list[Step] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {"title": self.title, "paragraphs": self.paragraphs, "tables": ([self.table] if self.table else []) + self.tables,
                "steps": [s.as_dict() for s in self.steps]}


class _Endpoint:
    def __init__(self, label: str, device: m.Device | None, port: str | None, vendors: dict[int, m.Vendor],
                 pops: dict[int, m.Pop]):
        self.label, self.device, self.port = label, device, port
        vendor = vendors.get(device.vendor_id) if device and device.vendor_id else None
        self.dialect = dialect_for(vendor.name if vendor else None, vendor.os_family if vendor else None,
                                   device.hostname if device else "")
        self.hostname = device.hostname if device else "(belum ditentukan)"
        self.pop = pops[device.pop_id].code if device and device.pop_id in pops else "-"
        self.loopback = device.loopback_ipv4 if device and device.loopback_ipv4 else "<loopback>"


def _fill(templates: list[str], **values: str) -> list[str]:
    return [t.format(**values) for t in templates]


def build_mop(db: Session, service: m.Service, meta: MopMeta,
              configs: dict[str, dict[str, list[str]]] | None = None) -> dict[str, Any]:
    """`configs` (Phase 7): {"A"/"B": {"config": [...], "rollback": [...]}} per endpoint."""
    vendors = {v.id: v for v in db.scalars(select(m.Vendor))}
    pops = {p.id: p for p in db.scalars(select(m.Pop))}
    customer = db.get(m.Customer, service.customer_id)
    a = _Endpoint("A", db.get(m.Device, service.a_device_id) if service.a_device_id else None, service.a_interface, vendors, pops)
    b = _Endpoint("B", db.get(m.Device, service.b_device_id) if service.b_device_id else None, service.b_interface, vendors, pops)
    design = design_for_service(db, service)
    report = validate(db, design)
    paths = _paths(db, design.primary_link_ids, design.backup_link_ids)
    draft = report["status"] == "fail"
    sections = [
        _purpose(service, customer, a, b),
        _summary(service, customer, a, b),
        _path_section(paths),
        _validation_section(report),
        _risk(service, _shares_port(db, service)),
        _preparation(service, a, b),
        _precheck(service, a, b, paths),
        _execution(service, a, b, configs),
        _verification(service, a, b),
        _rollback(service, a, b, configs),
        _contacts(meta),
    ]
    return {
        "title": f"Method of Procedure — {service.circuit_id}",
        "subtitle": f"Aktivasi layanan {service.service_type.upper()} {service.bandwidth_mbps / 1000:g} Gbps "
                    f"{a.pop} ↔ {b.pop} ({customer.name if customer else 'customer?'})",
        "draft": draft,
        "validation_status": report["status"],
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%MZ"),
        "meta": meta.__dict__,
        "sections": [s.as_dict() for s in sections],
    }


# --------------------------------------------------------------------------- sections


def _purpose(service: m.Service, customer, a: _Endpoint, b: _Endpoint) -> Section:
    return Section("1. Tujuan", [
        f"Mengaktifkan layanan {service.service_type.upper()} sebesar {service.bandwidth_mbps} Mbps untuk "
        f"{customer.name if customer else 'customer'} (circuit {service.circuit_id}) antara {a.hostname} ({a.pop}) dan "
        f"{b.hostname} ({b.pop}), proteksi {service.protection}.",
        "Dokumen ini berisi langkah pre-check, eksekusi, verifikasi, dan rollback per perangkat.",
    ])


def _summary(service: m.Service, customer, a: _Endpoint, b: _Endpoint) -> Section:
    rows = [
        ["Circuit ID", service.circuit_id],
        ["Customer", customer.name if customer else "-"],
        ["Layanan", f"{service.service_type.upper()} · {service.bandwidth_mbps} Mbps · proteksi {service.protection}"],
        ["Site A", f"{a.hostname} ({a.pop}) · port {a.port or '-'} · {DIALECT_LABELS.get(a.dialect or '', 'vendor ?')}"],
        ["Site B", f"{b.hostname} ({b.pop}) · port {b.port or '-'} · {DIALECT_LABELS.get(b.dialect or '', 'vendor ?')}"],
        ["VLAN", str(service.vlan_id) if service.vlan_id else "-"],
    ]
    if service.service_type == "l3vpn":
        rows += [["VRF", service.vrf_name or "-"], ["RD / RT", f"{service.route_distinguisher or '-'} / {service.route_target or '-'}"]]
    rows.append(["Status saat ini", service.status])
    return Section("2. Ringkasan layanan", table={"headers": ["Parameter", "Nilai"], "rows": rows})


def _paths(db: Session, primary: list[int], backup: list[int]) -> dict[str, list[m.Link]]:
    ids = set(primary) | set(backup)
    links = {l.id: l for l in db.scalars(select(m.Link).where(m.Link.id.in_(ids)))} if ids else {}
    return {"primary": [links[i] for i in primary if i in links], "backup": [links[i] for i in backup if i in links]}


def _path_section(paths: dict[str, list[m.Link]]) -> Section:
    section = Section("3. Jalur layanan")
    if not paths["primary"]:
        section.paragraphs.append("Belum ada jalur terencana. Hitung jalur di Customer Path sebelum MoP dipakai.")
        return section
    for role, label in (("primary", "Jalur utama"), ("backup", "Jalur backup")):
        if not paths[role]:
            continue
        rows = []
        for link in paths[role]:
            util = ((link.current_gbps or 0) + (link.reserved_gbps or 0)) / link.capacity_gbps * 100 if link.capacity_gbps else None
            rows.append([link.name, f"{link.capacity_gbps:g}G", f"{link.current_gbps or 0:.1f}G + {link.reserved_gbps or 0:.1f}G",
                         f"{util:.0f}%" if util is not None else "-", link.status])
        section.tables.append({"caption": label, "headers": ["Link", "Kapasitas", "Trafik + reserved", "Utilisasi", "Status"],
                               "rows": rows})
    return section


def _validation_section(report: dict[str, Any]) -> Section:
    verdict = {"pass": "LULUS — siap diimplementasikan.", "warn": "LULUS DENGAN PERINGATAN — tinjau temuan di bawah.",
               "fail": "BELUM LOLOS — dokumen ini DRAFT; jangan dieksekusi sebelum temuan GAGAL diselesaikan."}
    rows = [[{"fail": "GAGAL", "warn": "PERINGATAN"}[c["status"]], GROUPS[g["group"]], c["title"],
             " — ".join(p for p in (c["detail"], "; ".join(c["items"][:4])) if p)]
            for g in report["groups"] for c in g["checks"] if c["status"] in ("fail", "warn")]
    section = Section("4. Hasil validasi (Phase 5)", [
        f"Status: {verdict[report['status']]}",
        f"{report['counts']['fail']} gagal, {report['counts']['warn']} peringatan, {report['counts']['pass']} lulus.",
    ])
    if rows:
        section.table = {"headers": ["Status", "Kelompok", "Temuan", "Detail"], "rows": rows}
    return section


def _shares_port(db: Session, service: m.Service) -> bool:
    """Another live service on either access port of this one."""
    ends = [(d, i) for d, i in ((service.a_device_id, service.a_interface), (service.b_device_id, service.b_interface)) if d and i]
    if not ends:
        return False
    others = db.scalars(select(m.Service).where(m.Service.id != service.id, m.Service.status != "terminated"))
    return any((o.a_device_id, o.a_interface) in ends or (o.b_device_id, o.b_interface) in ends for o in others)


def _risk(service: m.Service, shared_port: bool) -> Section:
    paragraphs = [
        "Dampak ke layanan lain: tidak ada yang diharapkan. Konfigurasi hanya menambah sub-interface/layanan baru di port "
        "akses kedua ujung; jalur transport memakai link yang sudah ada.",
        "Risiko: salah port/VLAN dapat mengganggu layanan lain di port yang sama; salah konfigurasi VRF/RT dapat "
        "membocorkan rute antar-VPN. Mitigasi: pre-check port & VLAN, verifikasi sebelum menutup window, rollback siap.",
        "Estimasi durasi: 60 menit eksekusi + verifikasi; rollback ≤ 15 menit.",
    ]
    if shared_port:
        paragraphs.append("Catatan: port akses dipakai bersama layanan lain dengan VLAN berbeda — pastikan hanya sub-interface "
                          f"VLAN {service.vlan_id} yang diubah.")
    return Section("5. Risiko & dampak", paragraphs)


def _preparation(service: m.Service, a: _Endpoint, b: _Endpoint) -> Section:
    section = Section("6. Persiapan", [
        "Window maintenance dan nomor change disetujui; kontak customer dan NOC tersedia selama window.",
        "Laporan validasi (bagian 4) tidak memiliki temuan GAGAL.",
        "Backup konfigurasi setiap perangkat yang diubah disimpan sebelum eksekusi:",
    ])
    for ep in (a, b):
        section.steps.append(_step(ep, f"Backup konfigurasi {ep.hostname}", "backup", service, None))
    return section


def _values(service: m.Service, ep: _Endpoint, peer: _Endpoint | None) -> dict[str, str]:
    port = ep.port or "<port>"
    return {"port": port, "sub": f"{port}.{service.vlan_id}" if service.vlan_id else port, "vlan": str(service.vlan_id or ""),
            "vrf": service.vrf_name or "<vrf>", "lo": ep.loopback, "peer_lo": peer.loopback if peer else "<loopback peer>",
            "circuit": service.circuit_id, "circuit_tag": re.sub(r"[^A-Za-z0-9_-]+", "_", service.circuit_id)}


def _step(ep: _Endpoint, title: str, kind: str | list[str], service: m.Service, peer: _Endpoint | None, note: str = "") -> Step:
    if ep.dialect is None:
        return Step(ep.hostname, None, title, note="Vendor perangkat tidak dikenali: tulis perintah secara manual.")
    kinds = [kind] if isinstance(kind, str) else kind
    commands = [c for k in kinds for c in _fill(COMMANDS[ep.dialect][k], **_values(service, ep, peer))]
    return Step(ep.hostname, ep.dialect, title, commands, note)


def _precheck(service: m.Service, a: _Endpoint, b: _Endpoint, paths: dict[str, list[m.Link]]) -> Section:
    section = Section("7. Pre-check", [
        "Catat hasil setiap perintah. Hentikan dan eskalasi bila port down, VLAN sudah dipakai, atau adjacency IS-IS/LDP tidak lengkap.",
        NOTE_SYNTAX,
    ])
    for ep, peer in ((a, b), (b, a)):
        kinds = ["port", "core", "ping"] + (["vrf"] if service.service_type == "l3vpn" else [])
        if service.vlan_id:
            kinds.insert(1, "sub")
        section.steps.append(_step(ep, f"Pre-check {ep.hostname} (site {ep.label})", kinds, service, peer,
                                   "Sub-interface/VLAN belum boleh ada; port akses dalam keadaan admin up."))
    if paths["primary"]:
        section.paragraphs.append("Utilisasi link di jalur pada saat window harus di bawah 70% (lihat bagian 3).")
    return section


def _execution(service: m.Service, a: _Endpoint, b: _Endpoint, configs: dict[str, dict[str, list[str]]] | None) -> Section:
    section = Section("8. Eksekusi", ["Eksekusi berurutan: site A lalu site B. Simpan konfigurasi setelah setiap perangkat."])
    for ep in (a, b):
        config = (configs or {}).get(ep.label, {}).get("config")
        if ep.dialect is None:
            section.steps.append(Step(ep.hostname, None, f"Konfigurasi {ep.hostname}", note="Vendor tidak dikenali: konfigurasi manual."))
            continue
        commit = _fill(COMMANDS[ep.dialect]["commit"], **_values(service, ep, None))
        if config:
            section.steps.append(Step(ep.hostname, ep.dialect, f"Konfigurasi {ep.hostname} (site {ep.label})", config + commit))
        else:
            section.steps.append(Step(ep.hostname, ep.dialect, f"Konfigurasi {ep.hostname} (site {ep.label})", commit,
                                      "Konfigurasi layanan dibuat oleh Config Generator (Phase 7); terapkan sebelum perintah simpan di atas."))
    return section


def _verification(service: m.Service, a: _Endpoint, b: _Endpoint) -> Section:
    kind = {"l3vpn": "verify_l3", "internet": "verify_inet"}.get(service.service_type, "verify_l2")
    section = Section("9. Verifikasi", [
        "Layanan dinyatakan berhasil bila sub-interface up di kedua ujung, status layanan (VC/BGP) up, dan uji trafik "
        "end-to-end dengan customer berhasil (ping/throughput sesuai bandwidth).",
    ])
    for ep, peer in ((a, b), (b, a)):
        section.steps.append(_step(ep, f"Verifikasi {ep.hostname}", [kind, "ping"], service, peer))
    return section


def _rollback(service: m.Service, a: _Endpoint, b: _Endpoint, configs: dict[str, dict[str, list[str]]] | None) -> Section:
    section = Section("10. Rollback", [
        "Kriteria rollback: verifikasi gagal dan tidak teratasi dalam 30 menit, atau ada dampak ke layanan lain.",
        "Rollback dilakukan terbalik: site B lalu site A, kemudian pastikan kondisi kembali seperti hasil pre-check.",
    ])
    for ep in (b, a):
        undo = (configs or {}).get(ep.label, {}).get("rollback")
        if ep.dialect is None:
            section.steps.append(Step(ep.hostname, None, f"Rollback {ep.hostname}", note="Vendor tidak dikenali: rollback manual."))
            continue
        commands = (undo or []) + _fill(COMMANDS[ep.dialect]["rollback"], **_values(service, ep, None))
        section.steps.append(Step(ep.hostname, ep.dialect, f"Rollback {ep.hostname} (site {ep.label})", commands))
    return section


def _contacts(meta: MopMeta) -> Section:
    return Section("11. Pelaksana & eskalasi", table={"headers": ["Peran", "Nama", "Kontak"], "rows": [
        ["Pelaksana", meta.executor or "", ""],
        ["Reviewer", meta.reviewer or "", ""],
        ["Approver", meta.approver or "", ""],
        ["NOC", "", ""],
        ["Customer", "", ""],
    ]})
