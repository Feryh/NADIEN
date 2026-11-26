"""IPv6 / SRv6 addressing plan from the backbone team's register and the NADIEN inventory.

Rules (register workbook 'IPv6 Addressing.xlsx', sheets 01-03):
- Every router gets a /48 node locator 2400:dc00:RCCN::/48 inside its city block (R = region nibble, CC = city
  index, N = node index): loopback = locator::1, End SID = locator:e000::.
- The first quarter of a city block is the backbone band (standard 0x00-0x0F, metro 0x00-0x3F), the rest access.
- L2-only devices (OLT, switches) get no locator but an address from the city OAM /48 (2400:dc00:9RCC::/48).
Where the register points to sheets missing from the file (03A: per-function backbone bands, OAM layout) the
rules below are provisional and say so in the export.

Assignments are stored once saved, so an index stays put when devices are added, as long as the device stays in
the same city and band; 'confirmed' marks the ones agreed with the backbone team or already configured.
"""

import difflib
import io
import ipaddress
import math
import re
import unicodedata
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from typing import Any

import openpyxl
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import models as m
from .areas import area_for_pop

REGISTER_SHEET = "city register"
MAX_REGISTER_BYTES = 10_000_000

BACKBONE_ROLES = {"p", "bng", "pe"}
ACCESS_ROLES = {"csr"}
OAM_ROLES = {"olt", "switch", "access", "dci"}
SRV6_VENDORS = ("huawei", "cisco", "zte", "fiberhome", "juniper", "nokia")
BACKBONE_SIZE = {"STANDARD": 16, "METRO": 64}
CITY_SIZE = {"STANDARD": 64, "METRO": 256}
CITY_PREFIX = {"STANDARD": 42, "METRO": 40}
# PROVISIONAL (sheet 03A missing): backbone band per function. Standard BNG 0x04-0x07 is from the register example.
SUBBANDS = {
    "STANDARD": {"P/CORE": (0x00, 0x03), "BNG": (0x04, 0x07), "PE/PEIN/lainnya": (0x08, 0x0F)},
    "METRO": {"P/CORE": (0x00, 0x0F), "BNG": (0x10, 0x1F), "PE/PEIN/lainnya": (0x20, 0x3F)},
}
GROUP_ORDER = ("P/CORE", "BNG", "PE/PEIN/lainnya")
# PROVISIONAL OAM layout: one /64 per device group inside the city OAM /48.
OAM_SUBNETS = {"olt": 1, "switch": 2, "access": 2, "dci": 3}
OAM_OTHER = 4
OAM_LABELS = {1: "OLT", 2: "Switch / agregasi L2", 3: "DCI", 4: "Router non-SRv6 / leaf / manajemen"}

# NADIEN POP code -> register city code where the codes differ but the POP name proves the city.
ALIASES = {"BPN": "BPP", "YGK": "YGY", "MND": "MDO", "PAD": "PDG", "PAL": "PLU", "SMR": "SMD", "CBR": "CBB",
           "MTM": "MTR", "TRG": "KKR", "BGL": "BKL-SUM", "PWK": "PWT"}
# Jakarta POPs hold several register cities: the site token in the hostname decides.
JAKARTA_SITES = (("CSF", "JKT-CSF"), ("BSD", "JKT-BSD"), ("IDC", "JKT-IDC"), ("GR9", "JKT-GR9"), ("GRHA", "JKT-GR9"),
                 ("NDC", "JKT-NDC"))
AREA_REGION = {"Jabodetabek": "Jabodetabek", "Jawa Barat & Banten": "Java Ring West",
               "Jawa Tengah & DIY": "Java Ring West", "Jawa Timur": "Java Ring East", "Sumatera": "Sumatera",
               "Kalimantan": "Kalimantan", "Sulawesi": "Sulawesi", "Bali & Nusa Tenggara": "Bali Nusra"}
DECISIONS = ("city", "parent", "ignore", "review")


class PlanError(ValueError):
    """Bad register file or bad edit: 422."""


# --------------------------------------------------------------------------- register import

_COLUMNS = {"code": "City code", "name": "City", "region": "Region", "r": "R", "tier": "Tier", "index": "City index",
            "infra": "Infra / uSID city block", "res": "Residential city block", "cdn": "CDN city /48",
            "server": "Server city /48", "oam": "Internal-VRF / OAM /48"}
_REQUIRED = ("code", "name", "region", "r", "tier", "index", "infra")


def _number(value: Any) -> int:
    text = str(value).strip().lower()
    return int(text, 16) if text.startswith("0x") else int(float(text))


def _optional_net(value: Any) -> str | None:
    text = str(value or "").strip()
    if not text or text.lower() in ("n/a", "-"):
        return None
    return str(ipaddress.IPv6Network(text))


def _register_city(row: tuple, col: dict[str, int]) -> dict:
    get = lambda key: row[col[_COLUMNS[key]]] if _COLUMNS[key] in col and col[_COLUMNS[key]] < len(row) else None  # noqa: E731
    tier = str(get("tier") or "").strip().upper()
    if tier not in CITY_SIZE:
        raise ValueError(f"tier '{get('tier')}' bukan METRO/STANDARD")
    r, cc = _number(get("r")), _number(get("index"))
    block = ipaddress.IPv6Network(str(get("infra")).strip())
    if block.prefixlen != CITY_PREFIX[tier]:
        raise ValueError(f"blok {block} untuk kota {tier} harus /{CITY_PREFIX[tier]}")
    hextet3 = (int(block.network_address) >> 80) & 0xFFFF
    if hextet3 != (r << 12) | (cc << 4):
        raise ValueError(f"blok {block} tidak cocok dengan R=0x{r:x} dan index 0x{cc:02x}")
    return {"code": str(get("code")).strip(), "name": str(get("name")).strip(), "region": str(get("region")).strip(),
            "region_nibble": r, "tier": tier, "city_index": cc, "infra_block": str(block),
            "residential_block": _optional_net(get("res")), "cdn48": _optional_net(get("cdn")),
            "server48": _optional_net(get("server")), "oam48": _optional_net(get("oam"))}


def parse_register(data: bytes) -> list[dict]:
    """Cities from the 'City Register' sheet of the register workbook (cached values, formulas not evaluated)."""
    if len(data) > MAX_REGISTER_BYTES:
        raise PlanError("File register terlalu besar")
    try:
        workbook = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    except Exception as exc:  # openpyxl raises several unrelated types for a non-xlsx body
        raise PlanError("Bukan file Excel (.xlsx) yang bisa dibaca") from exc
    sheet = next((ws for ws in workbook.worksheets if REGISTER_SHEET in ws.title.lower()), None)
    if sheet is None:
        raise PlanError("Sheet 'City Register' tidak ditemukan di file")
    rows = sheet.iter_rows(values_only=True)
    header = next(rows, ())
    col = {str(h).strip(): i for i, h in enumerate(header) if h is not None}
    missing = [_COLUMNS[k] for k in _REQUIRED if _COLUMNS[k] not in col]
    if missing:
        raise PlanError(f"Kolom tidak ditemukan di City Register: {', '.join(missing)}")
    cities, errors = [], []
    for number, row in enumerate(rows, 2):
        if not row or col["City code"] >= len(row) or not row[col["City code"]] or not row[col["Infra / uSID city block"]]:
            continue
        try:
            cities.append(_register_city(row, col))
        except ValueError as exc:
            errors.append(f"baris {number}: {exc}")
    if errors:
        raise PlanError("Register tidak valid: " + "; ".join(errors[:5]))
    if not cities:
        raise PlanError("Tidak ada kota di sheet City Register")
    codes = [c["code"] for c in cities]
    duplicates = sorted({c for c in codes if codes.count(c) > 1})
    if duplicates:
        raise PlanError(f"Kode kota ganda di register: {', '.join(duplicates)}")
    return cities


def import_register(db: Session, cities: list[dict]) -> dict[str, Any]:
    existing = {c.code: c for c in db.scalars(select(m.Ipv6City))}
    created = updated = 0
    for data in cities:
        row = existing.get(data["code"])
        if row is None:
            db.add(m.Ipv6City(**data))
            created += 1
        else:
            for key, value in data.items():
                setattr(row, key, value)
            updated += 1
    db.commit()
    gone = sorted(set(existing) - {c["code"] for c in cities})
    return {"cities": len(cities), "created": created, "updated": updated, "not_in_file": gone}


# --------------------------------------------------------------------------- POP -> city


def _norm(text: str | None) -> str:
    text = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9 ]+", " ", text.lower()).strip()


def _similar(a: str, b: str) -> bool:
    return difflib.SequenceMatcher(None, a, b).ratio() >= 0.85


def _mentions(city: m.Ipv6City, pop_name: str | None) -> bool:
    words = _norm(pop_name).split()
    return any(_similar(w, pw) for w in _norm(city.name).split() if len(w) >= 4 for pw in words)


def _by_name(pop_name: str | None, cities: list[m.Ipv6City]) -> m.Ipv6City | None:
    words = _norm(pop_name).split()
    best, score = None, 0.0
    for city in cities:
        names = _norm(city.name).split()
        # The first distinctive word ("Purwokerto"); "Jakarta NDC" has none, so its first word stands in.
        first = next((w for w in names if len(w) >= 4 and w not in ("jakarta", "kota", "lippo")), names[0] if names else "")
        for word in words:
            ratio = difflib.SequenceMatcher(None, first, word).ratio()
            if ratio > score:
                best, score = city, ratio
    return best if score >= 0.85 else None


@dataclass
class PopCity:
    pop_id: int
    pop_code: str
    pop_name: str | None
    area: str | None
    devices: int
    city: str | None
    method: str  # manual | alias | exact | name | name (kode beda) | unmapped | ignored
    decision: str | None = None
    note: str = ""
    region: str | None = None
    suggested_city: str | None = None
    suggested_km: int | None = None


def _auto_city(code: str, name: str | None, cities: dict[str, m.Ipv6City]) -> tuple[str | None, str, str]:
    if code in ALIASES and ALIASES[code] in cities:
        target = cities[ALIASES[code]]
        return target.code, "alias", f"Kode NADIEN {code}, register {target.code} ({target.name})"
    exact = cities.get(code)
    named = _by_name(name, list(cities.values()))
    if exact:
        if named and named.code != code and _norm(name) != _norm(code) and not _mentions(exact, name):
            return named.code, "name (kode beda)", f"Kode {code} di register = {exact.name}; nama POP cocok {named.name}"
        return exact.code, "exact", ""
    if named:
        return named.code, "name", ""
    return None, "unmapped", ""


def _haversine_km(a: tuple[float, float], b: tuple[float, float]) -> float:
    lat1, lon1, lat2, lon2 = map(math.radians, (*a, *b))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 6371 * 2 * math.asin(math.sqrt(h))


def pop_cities(db: Session, cities: dict[str, m.Ipv6City]) -> dict[int, PopCity]:
    """Every POP with devices: its register city (planner decision first, else the automatic match)."""
    overrides = {row.pop_id: row for row in db.scalars(select(m.Ipv6PopMap))}
    counts = dict(db.execute(select(m.Device.pop_id, func.count()).where(m.Device.hidden.is_(False))
                             .group_by(m.Device.pop_id)).all())
    out: dict[int, PopCity] = {}
    for pop in db.scalars(select(m.Pop)):
        area = area_for_pop(pop.code, pop.region, pop.name)
        entry = PopCity(pop.id, pop.code, pop.name, area, counts.get(pop.id, 0), None, "unmapped")
        decision = overrides.get(pop.id)
        if decision is not None:
            entry.decision, entry.note = decision.decision, decision.note or ""
            entry.city = decision.city_code if decision.decision in ("city", "parent") else None
            entry.method = "manual" if entry.city else ("ignored" if decision.decision == "ignore" else "unmapped")
        else:
            entry.city, entry.method, entry.note = _auto_city(pop.code, pop.name, cities)
        entry.region = cities[entry.city].region if entry.city in cities else AREA_REGION.get(area or "")
        out[pop.id] = entry
    _suggest_parents(db, out, cities)
    return out


def _suggest_parents(db: Session, entries: dict[int, PopCity], cities: dict[str, m.Ipv6City]) -> None:
    coords = {pid: (lat, lon) for pid, lat, lon in db.execute(
        select(m.Pop.id, m.Pop.latitude, m.Pop.longitude).where(m.Pop.latitude.is_not(None), m.Pop.longitude.is_not(None)))}
    anchors = [(e, coords[e.pop_id]) for e in entries.values() if e.city in cities and e.pop_id in coords]
    for entry in entries.values():
        if entry.city or entry.method == "ignored" or entry.pop_id not in coords or not anchors:
            continue
        same = [(a, c) for a, c in anchors if cities[a.city].region == entry.region] or anchors
        nearest, where = min(same, key=lambda ac: _haversine_km(coords[entry.pop_id], ac[1]))
        entry.suggested_city, entry.suggested_km = nearest.city, round(_haversine_km(coords[entry.pop_id], where))


def set_pop_city(db: Session, pop_id: int, decision: str, city_code: str | None, note: str | None,
                 user: str) -> None:
    if db.get(m.Pop, pop_id) is None:
        raise LookupError("POP tidak ditemukan")
    if decision not in DECISIONS:
        raise PlanError(f"Keputusan tidak dikenal: {decision}")
    if decision in ("city", "parent"):
        if not city_code or db.scalar(select(m.Ipv6City.id).where(m.Ipv6City.code == city_code)) is None:
            raise PlanError(f"Kota {city_code} tidak ada di register")
    else:
        city_code = None
    row = db.scalar(select(m.Ipv6PopMap).where(m.Ipv6PopMap.pop_id == pop_id))
    if row is None:
        row = m.Ipv6PopMap(pop_id=pop_id, updated_by=user)
        db.add(row)
    row.decision, row.city_code, row.note, row.updated_by = decision, city_code, note, user
    db.commit()


def clear_pop_city(db: Session, pop_id: int) -> None:
    db.query(m.Ipv6PopMap).filter(m.Ipv6PopMap.pop_id == pop_id).delete()
    db.commit()


# --------------------------------------------------------------------------- devices


def classify(role: str | None, hostname: str, vendor: str | None) -> str:
    """backbone | access | oam | excluded (CPE and unknown roles)."""
    role, host, vendor = role or "", hostname.upper(), (vendor or "").lower()
    if role == "cpe":
        return "excluded"
    if role in BACKBONE_ROLES | ACCESS_ROLES | {"agg"} and not vendor.startswith(SRV6_VENDORS):
        return "oam"  # MikroTik management routers, Dell leafs, Raisecom/Extreme: no SRv6 node
    if role == "agg":
        return "backbone" if "PEIN" in host else "access"
    if role in BACKBONE_ROLES:
        return "backbone"
    if role in ACCESS_ROLES:
        return "access"
    return "oam" if role in OAM_ROLES else "excluded"


def backbone_group(role: str | None, hostname: str) -> str:
    host = hostname.upper()
    if role == "p" or host.startswith(("P1-", "P2-", "CORE")) or "-P1-" in host or "-P2-" in host:
        return "P/CORE"
    return "BNG" if role == "bng" else "PE/PEIN/lainnya"


def device_city(hostname: str, pop: PopCity | None, cities: dict[str, m.Ipv6City]) -> tuple[str | None, str]:
    if pop and pop.pop_code.startswith("JKT") and pop.method != "manual":
        tokens = re.split(r"[-_.]", hostname.upper())
        for token, code in JAKARTA_SITES:
            if token in tokens and code in cities:
                return code, f"site {token} di hostname"
    if pop and pop.city in cities:
        return pop.city, pop.method
    return None, "POP belum ada di register"


# --------------------------------------------------------------------------- addresses


def locator(city: m.Ipv6City, index: int) -> ipaddress.IPv6Network:
    base = ipaddress.IPv6Network(city.infra_block).network_address
    return ipaddress.IPv6Network((int(base) + (index << 80), 48))


def isis_area(city: m.Ipv6City, band: str) -> str:
    return "49.0001" if band == "backbone" else f"49.0{city.region_nibble:x}{city.city_index:02x}"


def oam48(city: m.Ipv6City) -> ipaddress.IPv6Network:
    if city.oam48:
        return ipaddress.IPv6Network(city.oam48)
    return ipaddress.IPv6Network((int(ipaddress.IPv6Address("2400:dc00::"))
                                  + ((0x9000 | city.region_nibble << 8 | city.city_index) << 80), 48))


def band_range(tier: str, band: str, group: str | None = None) -> range:
    if band == "backbone":
        if group:
            low, high = SUBBANDS[tier][group]
            return range(low, high + 1)
        return range(0, BACKBONE_SIZE[tier])
    return range(BACKBONE_SIZE[tier], CITY_SIZE[tier])


# --------------------------------------------------------------------------- the plan


@dataclass
class Plan:
    routers: list[dict] = field(default_factory=list)
    oam: list[dict] = field(default_factory=list)
    unmapped: list[dict] = field(default_factory=list)
    pops: list[PopCity] = field(default_factory=list)
    capacity: list[dict] = field(default_factory=list)
    excluded: dict[str, int] = field(default_factory=dict)
    cities: int = 0


def build_plan(db: Session) -> Plan:
    cities = {c.code: c for c in db.scalars(select(m.Ipv6City))}
    plan = Plan(cities=len(cities))
    if not cities:
        return plan
    pops = pop_cities(db, cities)
    plan.pops = sorted(pops.values(), key=lambda p: (-p.devices, p.pop_code))
    saved = {a.device_id: a for a in db.scalars(select(m.Ipv6Assignment))}
    rows = db.execute(select(m.Device.id, m.Device.hostname, m.Device.role, m.Device.pop_id, m.Device.loopback_ipv4,
                             m.Vendor.name).outerjoin(m.Vendor, m.Vendor.id == m.Device.vendor_id)
                      .where(m.Device.hidden.is_(False)).order_by(m.Device.hostname)).all()
    groups: dict[tuple[str, str], list[dict]] = defaultdict(list)
    excluded: Counter = Counter()
    for device_id, hostname, role, pop_id, loopback, vendor in rows:
        kind = classify(role, hostname, vendor)
        pop = pops.get(pop_id)
        if kind == "excluded" or (pop and pop.method == "ignored"):
            excluded[role or "-"] += 1
            continue
        device = {"device_id": device_id, "hostname": hostname, "role": role, "vendor": vendor, "loopback4": loopback,
                  "pop_code": pop.pop_code if pop else None, "kind": kind}
        code, how = device_city(hostname, pop, cities)
        if code is None:
            plan.unmapped.append({**device, "pop_name": pop.pop_name if pop else None,
                                  "region": pop.region if pop else None,
                                  "suggested_city": pop.suggested_city if pop else None})
            continue
        groups[(code, kind)].append({**device, "city": code, "how": how})
    plan.excluded = dict(excluded)
    used: dict[str, set[int]] = defaultdict(set)
    _keep_saved(groups, saved, cities, used)
    for (code, kind), devices in sorted(groups.items()):
        city = cities[code]
        if kind == "oam":
            plan.oam += _oam_rows(city, devices, saved)
        else:
            plan.routers += _router_rows(city, kind, devices, saved, used[code])
    order = {c.code: (c.region_nibble, c.city_index) for c in cities.values()}
    plan.routers.sort(key=lambda r: (order[r["city"]], r["node_index"] if r["node_index"] is not None else 1 << 16,
                                     r["hostname"]))
    plan.capacity = _capacity(cities, plan)
    return plan


def _keep_saved(groups: dict, saved: dict[int, m.Ipv6Assignment], cities: dict, used: dict[str, set[int]]) -> None:
    """Saved node indexes that still fit (same city, same band, inside the band) are reserved first."""
    for (code, kind), devices in groups.items():
        if kind == "oam":
            continue
        tier = cities[code].tier
        for device in devices:
            a = saved.get(device["device_id"])
            if (a and a.node_index is not None and a.city_code == code and a.band == kind
                    and a.node_index in band_range(tier, kind) and a.node_index not in used[code]):
                used[code].add(a.node_index)
                device["kept"] = a


def _router_rows(city: m.Ipv6City, kind: str, devices: list[dict], saved: dict, used: set[int]) -> list[dict]:
    rows = []
    for device in devices:
        group = backbone_group(device["role"], device["hostname"]) if kind == "backbone" else "AKSES"
        device["group"] = group
    order = sorted(devices, key=lambda d: (GROUP_ORDER.index(d["group"]) if d["group"] in GROUP_ORDER else 9,
                                           d["hostname"]))
    for device in order:
        kept: m.Ipv6Assignment | None = device.pop("kept", None)
        note = ""
        if kept is not None:
            index, status = kept.node_index, kept.status
        else:
            index, note = _free_index(city, kind, device["group"], used)
            status = "baru"
            moved = saved.get(device["device_id"])
            if moved is not None and moved.node_index is not None:
                note = (note + f" (sebelumnya {moved.city_code} 0x{moved.node_index:02X})").strip()
        rows.append(_router_row(city, kind, device, index, status, note))
    return rows


def _free_index(city: m.Ipv6City, kind: str, group: str, used: set[int]) -> tuple[int | None, str]:
    preferred = band_range(city.tier, kind, group if kind == "backbone" else None)
    for index in preferred:
        if index not in used:
            used.add(index)
            return index, ""
    for index in band_range(city.tier, kind):  # the function's sub-band is full: any free backbone slot
        if index not in used:
            used.add(index)
            return index, "sub-band penuh: dipindah ke slot bebas"
    return None, f"{'backbone' if kind == 'backbone' else 'access'} band kota penuh"


def _router_row(city: m.Ipv6City, kind: str, device: dict, index: int | None, status: str, note: str) -> dict:
    loc = locator(city, index) if index is not None else None
    return {"device_id": device["device_id"], "hostname": device["hostname"], "role": device["role"],
            "vendor": device["vendor"], "pop_code": device["pop_code"], "city": city.code, "region": city.region,
            "band": kind if loc else "overflow", "group": device["group"], "node_index": index,
            "locator": str(loc) if loc else None,
            "loopback6": f"{loc.network_address + 1}/128" if loc else None,
            "end_sid": str(ipaddress.IPv6Address(int(loc.network_address) + (0xE000 << 64))) if loc else None,
            "isis_area": isis_area(city, kind) if loc else None, "loopback4": device["loopback4"],
            "how": device["how"], "status": status, "note": note}


def _oam_rows(city: m.Ipv6City, devices: list[dict], saved: dict[int, m.Ipv6Assignment]) -> list[dict]:
    block = oam48(city)
    taken: set[str] = set()
    pending = []
    rows = []
    for device in devices:
        a = saved.get(device["device_id"])
        if a and a.city_code == city.code and a.band == "oam" and a.oam_address and a.oam_address not in taken:
            taken.add(a.oam_address)
            rows.append(_oam_row(city, block, device, a.oam_address, a.status))
        else:
            pending.append(device)
    next_host: Counter = Counter()
    for device in pending:
        subnet = OAM_SUBNETS.get(device["role"] or "", OAM_OTHER)
        while True:
            next_host[subnet] += 1
            address = str(ipaddress.IPv6Address(int(block.network_address) + (subnet << 64) + next_host[subnet]))
            if address not in taken:
                break
        taken.add(address)
        rows.append(_oam_row(city, block, device, address, "baru"))
    return rows


def _oam_row(city: m.Ipv6City, block: ipaddress.IPv6Network, device: dict, address: str, status: str) -> dict:
    subnet = (int(ipaddress.IPv6Address(address)) >> 64) & 0xFFFF
    net64 = ipaddress.IPv6Network((int(block.network_address) + (subnet << 64), 64))
    return {"device_id": device["device_id"], "hostname": device["hostname"], "role": device["role"],
            "vendor": device["vendor"], "pop_code": device["pop_code"], "city": city.code, "region": city.region,
            "oam48": str(block), "group": OAM_LABELS.get(subnet, "-"), "subnet": str(net64), "address": address,
            "how": device["how"], "status": status}


def _capacity(cities: dict[str, m.Ipv6City], plan: Plan) -> list[dict]:
    count: Counter = Counter((r["city"], r["band"]) for r in plan.routers)
    oam: Counter = Counter(r["city"] for r in plan.oam)
    out = []
    for city in sorted(cities.values(), key=lambda c: (c.region_nibble, c.city_index)):
        bb_size = BACKBONE_SIZE[city.tier]
        access_size = CITY_SIZE[city.tier] - bb_size
        backbone, access = count[(city.code, "backbone")], count[(city.code, "access")]
        out.append({"code": city.code, "name": city.name, "region": city.region, "tier": city.tier,
                    "city_index": f"0x{city.city_index:02X}", "infra_block": city.infra_block,
                    "backbone": backbone, "backbone_size": bb_size,
                    "backbone_fit": "OK" if backbone <= bb_size else "PENUH",
                    "access": access, "access_size": access_size,
                    "fit_50": "OK" if access * 2 <= access_size else "PROMOTE - needs a second city block",
                    "overflow": count[(city.code, "overflow")], "oam": oam[city.code], "oam48": str(oam48(city)),
                    "residential_block": city.residential_block})
    return out


def summary(plan: Plan) -> dict[str, Any]:
    bands = Counter(r["band"] for r in plan.routers)
    conflicts = [p for p in plan.pops if p.method in ("alias", "name (kode beda)") and p.devices]
    return {"cities": plan.cities, "routers": bands["backbone"] + bands["access"], "backbone": bands["backbone"],
            "access": bands["access"], "overflow": bands["overflow"], "oam": len(plan.oam),
            "unmapped_devices": len(plan.unmapped),
            "unmapped_pops": sum(1 for p in plan.pops if p.devices and not p.city and p.method != "ignored"),
            "code_conflicts": len(conflicts),
            "promote": [c["code"] for c in plan.capacity if c["fit_50"] != "OK"],
            "backbone_full": [c["code"] for c in plan.capacity if c["backbone_fit"] != "OK"],
            "unsaved": sum(1 for r in plan.routers + plan.oam if r["status"] == "baru" and r.get("band") != "overflow"),
            "excluded": plan.excluded}


def pop_view(p: PopCity) -> dict[str, Any]:
    return asdict(p)


# --------------------------------------------------------------------------- saving


def save_plan(db: Session, user: str) -> dict[str, int]:
    """Store the current proposal: new rows become 'proposed', confirmed rows stay, stale proposals go."""
    plan = build_plan(db)
    saved = {a.device_id: a for a in db.scalars(select(m.Ipv6Assignment))}
    target: dict[int, dict] = {}
    for row in plan.routers:
        if row["band"] != "overflow":
            target[row["device_id"]] = {"city_code": row["city"], "band": row["band"], "node_index": row["node_index"],
                                        "oam_address": None}
    for row in plan.oam:
        target[row["device_id"]] = {"city_code": row["city"], "band": "oam", "node_index": None,
                                    "oam_address": row["address"]}
    dropped = 0
    for device_id, a in saved.items():  # first free what moves, so the unique keys never clash mid-way
        wanted = target.get(device_id)
        if wanted is None or any(getattr(a, k) != v for k, v in wanted.items()):
            db.delete(a)
            dropped += wanted is None
    db.flush()
    written = 0
    for device_id, wanted in target.items():
        a = saved.get(device_id)
        if a is not None and all(getattr(a, k) == v for k, v in wanted.items()):
            continue
        db.add(m.Ipv6Assignment(device_id=device_id, status="proposed", assigned_by=user, **wanted))
        written += 1
    db.commit()
    return {"written": written, "dropped": dropped, "total": len(target)}


def set_status(db: Session, device_id: int, status: str) -> m.Ipv6Assignment:
    if status not in ("proposed", "confirmed"):
        raise PlanError("Status harus proposed atau confirmed")
    a = db.scalar(select(m.Ipv6Assignment).where(m.Ipv6Assignment.device_id == device_id))
    if a is None:
        raise LookupError("Perangkat belum punya alokasi tersimpan: simpan rencana dulu")
    a.status = status
    db.commit()
    return a
