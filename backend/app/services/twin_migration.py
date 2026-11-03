"""Digital twin (Phase 10): IS-IS / SRv6 migration readiness per area, with a suggested wave order.

Per router (roles p, pe, agg, bng, dci):
- ready: loopback, IS-IS NET and an SRv6 locator in the inventory;
- needs_config: loopback and IS-IS NET, no SRv6 locator yet;
- blocked: no loopback or no IS-IS NET (the underlay must be fixed first).
A software version marked not-recommended is reported as a blocker too.

Waves go area by area: fewest blocked routers and fewest services first, so the first waves are low-risk and
teach the procedure. Inside an area core (P/agg) goes before the edge (PE/BNG/DCI), so the transport supports
SRv6 before the services on top of it move. Services stay on LDP until both of their PEs are ready.
"""

from collections import defaultdict
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models as m
from .areas import UNMAPPED_AREA, area_for_pop
from .vendors import DIALECT_LABELS, dialect_for

ROUTER_ROLES = ("p", "pe", "agg", "bng", "dci")
CORE_ROLES = ("p", "agg")


def _routers(db: Session) -> list[dict[str, Any]]:
    pops = {p.id: p for p in db.execute(select(m.Pop.id, m.Pop.code, m.Pop.region, m.Pop.name))}
    vendors = {v.id: v for v in db.execute(select(m.Vendor.id, m.Vendor.name, m.Vendor.os_family))}
    software = {s.id: s for s in db.execute(select(m.SoftwareVersion.id, m.SoftwareVersion.version,
                                                   m.SoftwareVersion.vendor_id, m.SoftwareVersion.is_recommended))}
    # Like the DEV-SW validation check: "not recommended" only means something once the vendor has a recommendation.
    curated_vendors = {s.vendor_id for s in software.values() if s.is_recommended}
    rows = []
    for d in db.execute(select(m.Device.id, m.Device.hostname, m.Device.pop_id, m.Device.role, m.Device.vendor_id,
                               m.Device.software_id, m.Device.loopback_ipv4, m.Device.isis_net, m.Device.srv6_locator)
                        .where(m.Device.role.in_(ROUTER_ROLES), m.Device.status != "decommissioned")):
        pop = pops.get(d.pop_id)
        vendor = vendors.get(d.vendor_id)
        sw = software.get(d.software_id)
        blockers = [label for missing, label in ((not d.loopback_ipv4, "tanpa loopback"), (not d.isis_net, "tanpa IS-IS NET"),
                                                 (sw is not None and not sw.is_recommended
                                                  and sw.vendor_id in curated_vendors, "software bukan rekomendasi"))
                    if missing]
        state = "blocked" if not d.loopback_ipv4 or not d.isis_net else "ready" if d.srv6_locator else "needs_config"
        dialect = dialect_for(vendor.name if vendor else None, vendor.os_family if vendor else None, d.hostname)
        rows.append({"device_id": d.id, "hostname": d.hostname, "role": d.role, "pop": pop.code if pop else None,
                     "area": area_for_pop(pop.code, pop.region, pop.name) if pop else UNMAPPED_AREA,
                     "vendor": DIALECT_LABELS.get(dialect or "", vendor.name if vendor else "-"),
                     "software": sw.version if sw else None, "state": state, "blockers": blockers,
                     "tier": "core" if d.role in CORE_ROLES else "edge"})
    return rows


def readiness(db: Session) -> dict[str, Any]:
    routers = _routers(db)
    state_of = {r["device_id"]: r["state"] for r in routers}
    area_of = {r["device_id"]: r["area"] for r in routers}
    services = db.execute(select(m.Service.a_device_id, m.Service.b_device_id)
                          .where(m.Service.status != "terminated")).all()
    per_area: dict[str, dict[str, Any]] = defaultdict(lambda: {"routers": 0, "ready": 0, "needs_config": 0,
                                                              "blocked": 0, "core": 0, "edge": 0, "services": 0,
                                                              "services_ready": 0})
    for r in routers:
        a = per_area[r["area"]]
        a["routers"] += 1
        a[r["state"]] += 1
        a[r["tier"]] += 1
    for svc in services:
        for area in {area_of.get(svc.a_device_id), area_of.get(svc.b_device_id)} - {None}:
            per_area[area]["services"] += 1
            # A service can leave LDP only when both of its PEs speak SRv6.
            if state_of.get(svc.a_device_id) == "ready" and state_of.get(svc.b_device_id) == "ready":
                per_area[area]["services_ready"] += 1
    rows = [{"area": area, **a, "ready_pct": round(a["ready"] / a["routers"] * 100, 1) if a["routers"] else 0.0,
             "blocked_pct": round(a["blocked"] / a["routers"] * 100, 1) if a["routers"] else 0.0}
            for area, a in per_area.items()]
    rows.sort(key=lambda a: (a["area"] == UNMAPPED_AREA, a["blocked_pct"], a["services"], -a["routers"]))
    for wave, row in enumerate(rows, start=1):
        row["wave"] = wave
    blockers: dict[str, int] = defaultdict(int)
    for r in routers:
        for b in r["blockers"]:
            blockers[b] += 1
    total = len(routers)
    return {
        "totals": {"routers": total, **{s: sum(1 for r in routers if r["state"] == s)
                                        for s in ("ready", "needs_config", "blocked")}},
        "blockers": [{"label": k, "routers": v} for k, v in sorted(blockers.items(), key=lambda kv: -kv[1])],
        "areas": rows,
        "notes": [
            "Urutan wave: area dengan router terblokir paling sedikit dan layanan paling sedikit lebih dulu.",
            "Di dalam satu area: core (P/agg) dulu, baru edge (PE/BNG/DCI).",
            "Selama transisi LDP dan SRv6 berjalan bersamaan; layanan tetap di LDP sampai kedua PE-nya siap SRv6.",
        ],
    }


def area_devices(db: Session, area: str) -> list[dict[str, Any]]:
    """Routers of one area in migration order: blocked last, core before edge."""
    order = {"ready": 0, "needs_config": 1, "blocked": 2}
    rows = [r for r in _routers(db) if r["area"] == area]
    return sorted(rows, key=lambda r: (order[r["state"]], r["tier"] != "core", r["hostname"]))
