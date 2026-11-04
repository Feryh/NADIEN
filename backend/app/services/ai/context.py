"""Network data sent with each question, picked from what the question mentions.

Retrieval instead of tool calling, so any model works — including small local ones that do not do tools:
- always a short network summary;
- devices, POPs, circuits and areas named in the question (hostname / code / circuit id / area name);
- topic sections when the question is about hotspots, forecasts, data health or services/validation.
Sections are added in that order until the character budget is used up.
"""

import re
from dataclasses import dataclass, field
from typing import Any, Callable

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from ... import models as m
from ...config import settings
from ..areas import AREA_ORDER, area_for_pop
from ..capacity import hotspot_level
from ..validation import design_for_service, validate

TOPICS: dict[str, re.Pattern[str]] = {
    "hotspot": re.compile(r"hotspot|padat|kritis|critical|penuh|utili[sz]a|congest|tinggi|busy|warning", re.I),
    "forecast": re.compile(r"forecast|prediksi|habis|tren|trend|growth|pertumbuhan|kapan|exhaust|upgrade", re.I),
    "health": re.compile(r"kelengkapan|lengkap|sync|koordinat|loopback|lldp|is-?is|kualitas data|data health|mtu", re.I),
    "services": re.compile(r"layanan|service|validasi|validation|mop|draft|pelanggan|customer|circuit", re.I),
}
_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_./:-]{2,}")


@dataclass
class Context:
    text: str
    sources: list[str] = field(default_factory=list)


def build(db: Session, question: str, budget: int | None = None) -> Context:
    budget = budget or settings.ai_context_chars
    tokens = {t.strip(".:,").upper() for t in _TOKEN.findall(question)}
    sections: list[tuple[str, Callable[[], str]]] = [("Ringkasan jaringan", lambda: _summary(db))]
    sections += [(f"Device {h}", (lambda d=d: _device(db, d))) for h, d in _devices(db, tokens).items()]
    sections += [(f"POP {p.code}", (lambda p=p: _pop(db, p))) for p in _pops(db, tokens)]
    sections += [(f"Layanan {s.circuit_id}", (lambda s=s: _service(db, s))) for s in _services(db, tokens)]
    areas = [a for a in AREA_ORDER if a.lower() in question.lower()]
    sections += [(f"Area {a}", (lambda a=a: _area(db, a))) for a in areas]
    hotspot_title = "Hotspot link" + (f" ({', '.join(areas)})" if areas else "")
    topic_sections = {"hotspot": (hotspot_title, lambda: _hotspots(db, areas)),
                      "forecast": ("Forecast per area", lambda: _forecast(db)),
                      "health": ("Kesehatan data", lambda: _health(db)),
                      "services": ("Layanan & validasi", lambda: _services_summary(db))}
    sections += [topic_sections[k] for k, pattern in TOPICS.items() if pattern.search(question)]
    parts: list[str] = []
    sources: list[str] = []
    used = 0
    for title, render in sections:
        body = render()
        if not body:
            continue
        block = f"## {title}\n{body}\n"
        if used + len(block) > budget:
            if not parts:  # always send at least a truncated summary
                parts.append(block[:budget])
                sources.append(title)
            break
        parts.append(block)
        sources.append(title)
        used += len(block)
    return Context("\n".join(parts), sources)


# --------------------------------------------------------------------------- lookups


def _devices(db: Session, tokens: set[str]) -> dict[str, m.Device]:
    hostnames = [t for t in tokens if "-" in t]
    if not hostnames:
        return {}
    rows = db.scalars(select(m.Device).where(func.upper(m.Device.hostname).in_(hostnames)).limit(5)).all()
    if len(rows) < 5:  # partial names: "JKT-NDC-BNG" matches JKT-NDC-BNG1-M4, JKT-NDC-BNG2-M4
        partial = [t for t in hostnames if t.count("-") >= 2 and t not in {r.hostname.upper() for r in rows}]
        if partial:
            rows += db.scalars(select(m.Device).where(or_(*(func.upper(m.Device.hostname).like(f"{t}%")
                                                          for t in partial))).limit(5 - len(rows))).all()
    return {d.hostname: d for d in rows}


def _pops(db: Session, tokens: set[str]) -> list[m.Pop]:
    codes = [t for t in tokens if t.isalpha() and 3 <= len(t) <= 5]
    return db.scalars(select(m.Pop).where(m.Pop.code.in_(codes)).limit(5)).all() if codes else []


def _services(db: Session, tokens: set[str]) -> list[m.Service]:
    return db.scalars(select(m.Service).where(func.upper(m.Service.circuit_id).in_(tokens)).limit(3)).all() \
        if tokens else []


# --------------------------------------------------------------------------- sections


def _summary(db: Session) -> str:
    from ...routers.dashboard import summary  # late: routers import services, not the other way round

    s = summary(db)
    c, cap = s["counts"], s["capacity"]
    colors = s["link_colors"]
    return (f"{c['pops']} POP, {c['devices']} perangkat, {c['interfaces']} interface, {c['links']} link "
            f"({s['links_down']} down), {c['services']} layanan aktif untuk {c['customers']} customer.\n"
            f"Kapasitas total {cap['total_gbps']} Gbps, trafik {cap['current_gbps']} Gbps "
            f"(utilisasi {cap['utilization_pct']}%), reserved {cap['reserved_gbps']} Gbps.\n"
            f"Warna kapasitas link (committed %): hijau {colors.get('green', 0)}, kuning {colors.get('yellow', 0)}, "
            f"merah {colors.get('red', 0)}. Ambang hotspot: warning ≥ {settings.hotspot_warning_pct:g}%, "
            f"critical ≥ {settings.hotspot_critical_pct:g}%.")


def _links_of(db: Session, hostname: str) -> list[Any]:
    from ...routers.capacity import all_link_capacity

    return [l for l in all_link_capacity(db) if hostname in (l.a_device, l.b_device)]


def _device(db: Session, device: m.Device) -> str:
    pop = db.get(m.Pop, device.pop_id)
    vendor = db.get(m.Vendor, device.vendor_id) if device.vendor_id else None
    links = sorted(_links_of(db, device.hostname), key=lambda l: -l.utilization_pct)
    vendor_label = f"{vendor.name} {vendor.os_family or ''}".strip() if vendor else "-"
    lines = [f"POP {pop.code if pop else '-'} ({area_for_pop(pop.code, pop.region, pop.name) if pop else '-'}), "
             f"vendor {vendor_label}, role {device.role}, "
             f"status {device.status}, loopback {device.loopback_ipv4 or 'tidak ada'}, "
             f"IS-IS NET {device.isis_net or 'tidak ada'}, {len(links)} link."]
    for l in links[:8]:
        peer = l.b_device if l.a_device == device.hostname else l.a_device
        lines.append(f"- ke {peer}: {l.capacity_gbps:g}G, trafik {l.current_gbps:.1f}G, utilisasi {l.utilization_pct}% "
                     f"({hotspot_level(l.utilization_pct)}), status {l.status}"
                     + (f", ke ambang {l.months_to_threshold:.1f} bln" if l.months_to_threshold is not None else ""))
    return "\n".join(lines)


def _pop(db: Session, pop: m.Pop) -> str:
    from ...routers.capacity import pop_utilization

    row = next((r for r in pop_utilization(db) if r["pop_id"] == pop.id), None)
    head = (f"{pop.name}, area {area_for_pop(pop.code, pop.region, pop.name)}, koordinat "
            f"{'ada' if pop.latitude is not None else 'belum ada'}.")
    if row is None:
        return head
    return (head + f"\n{row['devices']} perangkat, {row['links']} link, kapasitas {row['capacity_gbps']:g}G, "
            f"trafik {row['current_gbps']:.1f}G, committed {row['committed_pct']}%.")


def _service(db: Session, service: m.Service) -> str:
    report = validate(db, design_for_service(db, service))
    a = db.get(m.Device, service.a_device_id) if service.a_device_id else None
    b = db.get(m.Device, service.b_device_id) if service.b_device_id else None
    failed = [f"{c['title']} ({c['status']})" for g in report["groups"] for c in g["checks"] if c["status"] in ("fail", "warn")]
    return (f"{service.service_type.upper()} {service.bandwidth_mbps} Mbps, status {service.status}, proteksi "
            f"{service.protection}, VLAN {service.vlan_id or '-'}, VRF {service.vrf_name or '-'}.\n"
            f"Site A {a.hostname if a else '-'} port {service.a_interface or '-'}; site B {b.hostname if b else '-'} "
            f"port {service.b_interface or '-'}.\nValidasi: {report['status']}"
            + (f" — {'; '.join(failed[:8])}" if failed else ""))


def _area(db: Session, area: str) -> str:
    from ...routers.insights import areas

    row = next((r for r in areas(db)["areas"] if r["area"] == area), None)
    if row is None:
        return ""
    return (f"{row['pops']} POP ({row['located_pops']} berkoordinat), {row['links']} link, kapasitas "
            f"{row['capacity_gbps']:g}G, committed {row['committed_pct']}%, link kritis {row['critical']}, "
            f"warning {row['warning']}, link terburuk {row['max_utilization_pct']}%.")


def _hotspots(db: Session, areas: list[str]) -> str:
    """Busiest links; only those touching the named areas when the question names any."""
    from ...routers.capacity import all_link_capacity

    links = [l for l in all_link_capacity(db) if l.capacity_gbps > 0
             and hotspot_level(l.utilization_pct) in ("critical", "warning")]
    if areas:
        area_of = {p.code: area_for_pop(p.code, p.region, p.name)
                   for p in db.execute(select(m.Pop.code, m.Pop.region, m.Pop.name))}
        links = [l for l in links if {area_of.get(l.a_pop or ""), area_of.get(l.b_pop or "")} & set(areas)]
    links.sort(key=lambda l: -l.utilization_pct)
    lines = [f"{len(links)} link warning/critical. Teratas:"]
    lines += [f"- {l.a_device} ↔ {l.b_device} ({l.a_pop or '-'}–{l.b_pop or '-'}): {l.capacity_gbps:g}G, "
              f"utilisasi {l.utilization_pct}% ({hotspot_level(l.utilization_pct)})" for l in links[:12]]
    return "\n".join(lines)


def _forecast(db: Session) -> str:
    from ...routers.insights import trends

    data = trends(db)
    lines = [f"Histori utilisasi tersedia {data['network']['history_days']} hari (forecast butuh ≥ 30 hari)."]
    for a in data["areas"]:
        months = f"{a['months_to_threshold']:.1f} bln ({a['threshold_date']})" if a["months_to_threshold"] is not None \
            else "tidak terprediksi"
        lines.append(f"- {a['label']}: committed {a['committed_pct']}%, ke ambang {months}")
    return "\n".join(lines)


def _health(db: Session) -> str:
    from ...routers.insights import data_health

    data = data_health(db)
    lines = [f"- {c['label']}: {c['count']} dari {c['total']}" for c in data["checks"] if c["severity"] != "ok"]
    lines += [f"- sync {s['kind']}: " + (f"{s.get('age_hours')} jam lalu" if s["last_ok"] else "tidak ada catatan job")
              for s in data["syncs"]]
    return "\n".join(lines)


def _services_summary(db: Session) -> str:
    from ...routers.insights import services

    data = services(db)
    v = data["validation"]
    lines = [f"{data['total']} layanan aktif; per tipe {data['by_type'] or '-'}; validasi gagal {v['fail']}, "
             f"peringatan {v['warn']}, lulus {v['pass']}."]
    lines += [f"- MoP DRAFT {d['circuit_id']}: {', '.join(d['failed'])}" for d in data["draft_mops"][:8]]
    return "\n".join(lines)
