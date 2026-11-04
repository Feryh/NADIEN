"""Coordinates for POPs from KML/KMZ placemarks (Google Earth exports of POP / OLT / ODC sites).

Placemarks are named after locations ("NDC Palembang", "MTI Kisaran", "OLT Telanaipura"), not after POP
codes, so a POP is matched through its city: the city of its code (BDG -> bandung, siblings included),
or a city word in the POP's name/location ("Bukit Tinggi - Sumatra Barat"). A candidate must lie inside
the POP's area box. Among several placemarks of one city the main site wins (NDC / MTI / Branch / Data
Centre), then the one named exactly after the city.
"""

import io
import math
import re
import zipfile
from dataclasses import dataclass
from typing import Any
from xml.etree import ElementTree as ET

from .areas import CITIES, CITY_ALIASES, FUNCTION_TOKENS, in_area

MAX_ARCHIVE_BYTES = 50_000_000  # uncompressed total: KMZ exports of POP lists are a few MB at most
MAX_NESTING = 3


class KmlError(ValueError):
    pass


@dataclass
class Placemark:
    name: str
    lat: float
    lon: float
    folder: str | None = None
    source: str | None = None


# --------------------------------------------------------------------------- reading


def read_placemarks(data: bytes, source: str = "") -> list[Placemark]:
    """Point placemarks from KML, KMZ, or a zip holding either (POP EMR.kmz.zip)."""
    out: list[Placemark] = []
    for name, kml in _kml_documents(data, source, depth=0, budget=[MAX_ARCHIVE_BYTES]):
        out.extend(_parse_kml(kml, name))
    if not out:
        raise KmlError("Tidak ada placemark titik (Point) di file ini")
    return out


def _kml_documents(data: bytes, label: str, depth: int, budget: list[int]):
    if not zipfile.is_zipfile(io.BytesIO(data)):
        yield label, data
        return
    if depth >= MAX_NESTING:
        raise KmlError("Arsip bersarang terlalu dalam")
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        for info in archive.infolist():
            lower = info.filename.lower()
            if not lower.endswith((".kml", ".kmz", ".zip")):
                continue  # icons, images
            budget[0] -= info.file_size
            if budget[0] < 0:
                raise KmlError("Isi arsip terlalu besar")
            content = archive.read(info)
            if lower.endswith(".kml"):
                yield f"{label}/{info.filename}", content
            else:
                yield from _kml_documents(content, f"{label}/{info.filename}", depth + 1, budget)


def _tag(element: ET.Element) -> str:
    return element.tag.rsplit("}", 1)[-1]


def _parse_kml(kml: bytes, source: str) -> list[Placemark]:
    # KML never needs a DTD; refusing one rules out entity-expansion attacks before parsing.
    if b"<!DOCTYPE" in kml[:4096].upper() or b"<!ENTITY" in kml.upper():
        raise KmlError("File KML dengan DOCTYPE/ENTITY tidak diterima")
    try:
        root = ET.fromstring(kml)
    except ET.ParseError as exc:
        raise KmlError(f"KML tidak valid: {exc}") from exc
    out: list[Placemark] = []
    _walk(root, None, source, out)
    return out


def _walk(element: ET.Element, folder: str | None, source: str, out: list[Placemark]) -> None:
    for child in element:
        kind = _tag(child)
        if kind == "Folder":
            name = next((c.text for c in child if _tag(c) == "name"), None)
            _walk(child, (name or "").strip() or folder, source, out)
        elif kind == "Placemark":
            placemark = _placemark(child, folder, source)
            if placemark:
                out.append(placemark)
        else:
            _walk(child, folder, source, out)


def _placemark(element: ET.Element, folder: str | None, source: str) -> Placemark | None:
    name = next((c.text for c in element if _tag(c) == "name"), None)
    point = next((p for p in element.iter() if _tag(p) == "Point"), None)
    coords = next((c.text for c in point.iter() if _tag(c) == "coordinates"), None) if point is not None else None
    if not name or not coords:
        return None
    try:
        lon, lat = (float(v) for v in coords.strip().split()[0].split(",")[:2])
    except ValueError:
        return None
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None
    return Placemark(name=name.strip(), lat=round(lat, 7), lon=round(lon, 7), folder=folder, source=source)


# --------------------------------------------------------------------------- matching


_SITE_PREFIX = re.compile(r"^(olt|odc|mti|ndc|branch|pop|sto|bbu|data centre|data center|kantor|office|server room|kota)\s+")
# Words that only narrow a city down ("Bogor Utara", "Samarinda Ulu"): still that city.
_DIRECTION_WORDS = {"utara", "selatan", "barat", "timur", "tengah", "pusat", "kota", "ilir", "ulu", "hulu", "hilir"}
# 4th letter of a sub-POP code (JKTU, BKSB, CKRT): which part of the city it is.
_SUB_POP_DIRECTION = {"U": "utara", "S": "selatan", "B": "barat", "T": "timur", "P": "pusat"}
_MAIN_SITE = re.compile(r"^(ndc|mti|branch|data centre|data center|pop)\b")
_CITY_NAMES = sorted({n for n, _ in CITIES.values()} | set(CITY_ALIASES), key=len, reverse=True)


def _words(text: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()


def _key(name: str) -> str:
    """'NDC Palembang' -> 'palembang', 'ODC Branch Garut' -> 'branch garut' -> 'garut'."""
    key = _words(name)
    for _ in range(2):
        key = _SITE_PREFIX.sub("", key)
    return key


def _city_of_code(code: str) -> str | None:
    entry = CITIES.get(code.upper()) or CITIES.get(code.upper()[:3])
    return entry[0] if entry else None


def _city_in(text: str | None) -> str | None:
    padded = f" {_words(text)} "
    return next((c for c in _CITY_NAMES if f" {c} " in padded), None)


def _location_city(name: str | None, code: str) -> str | None:
    """Observium locations read 'City - District' ('Kisaran - Kota', 'Bukit Tinggi - Sumatra Barat'):
    the part before the dash, for cities the dictionary does not list."""
    phrase = _words(re.split(r"\s[-–]\s|_|,", name or "")[0])
    return phrase if len(phrase) >= 4 and phrase != code.lower() else None


@dataclass
class _Candidate:
    placemark: Placemark
    confidence: str  # high | medium | low
    rank: tuple


def _candidate(pm: Placemark, city: str, direction: str | None = None) -> _Candidate | None:
    key = _key(pm.name)
    words = key.split()
    city_words = city.split()
    if direction and direction not in words:
        return None  # JKTU needs a "... Utara" placemark; the city-wide point belongs to JKT
    rest = words[len(city_words):] if words[:len(city_words)] == city_words else None
    if key == city:
        confidence = "high"
    elif rest is not None and set(rest) <= _DIRECTION_WORDS:
        confidence = "high" if direction else "medium"  # "Bekasi Utara" for BKSU; "Bogor Utara" for BGR
    elif rest is not None or f" {city} " in f" {key} ":
        confidence = "low"  # "Jakarta Garden City", "Gunung Pati": the city word, but maybe another place
    else:
        return None
    main = bool(_MAIN_SITE.match(_words(pm.name)))
    if main and confidence == "low":
        confidence = "medium"  # "Data Centre STO Telkom Balikpapan"
    order = {"high": 0, "medium": 1, "low": 2}[confidence]
    return _Candidate(pm, confidence, (order, not main, len(key), pm.name))


def _pop_city(pop: dict[str, Any]) -> tuple[str | None, str | None]:
    """(city, direction for sub-POPs). A city named in the POP's own name/location wins over the
    city of its code (POP PDG named 'Pandeglang-…' is Pandeglang, not Padang)."""
    code = pop["code"].upper()
    named = _city_in(pop.get("name")) or _city_in(pop.get("city"))
    code_city = _city_of_code(code)
    city = named or code_city or _location_city(pop.get("name"), code)
    direction = None
    if len(code) == 4 and code[:3] in CITIES and code not in CITIES and city == _city_of_code(code[:3]):
        direction = _SUB_POP_DIRECTION.get(code[3])
        if direction is None:
            return None, None  # BGRK, JKTL: part of the city we cannot place
    return city, direction


# --------------------------------------------------------------------------- devices (site level)

SITE_RADIUS_KM = 80  # a site placemark must lie this close to the device's POP (or its city centre)
SAME_SITE_KM = 2  # candidates this close together are one site under different names
# Words that name a building or facility, not the place an abbreviation is made from.
_FACILITY_WORDS = re.compile(
    r"^(olt|odc|mti|ndc|branch|pop|sto|bbu|data centre|data center|server room|hrb|shelter|kantor|office|the)\s+")
# Beyond these distances from the device's POP a matching name is more likely another place.
SITE_SURE_KM = 30
SITE_DOUBTFUL_KM = 60
# Site tokens that are facility names in hostnames (besides the POP function words): matched literally.
_LITERAL_SITES = {"IDC", "IIX", "CSF", "GR9", "DCI", "NOC"}


def km_between(a: tuple[float, float], b: tuple[float, float]) -> float:
    dlat = math.radians(b[0] - a[0])
    dlon = math.radians(b[1] - a[1]) * math.cos(math.radians((a[0] + b[0]) / 2))
    return 6371 * math.hypot(dlat, dlon)


def _site_key(name: str) -> str:
    key = re.sub(r"\([^)]*\)", " ", name.lower())  # "(Bukit Raya)" notes
    key = " ".join(re.sub(r"[^a-z ]+", " ", key).split())
    for _ in range(3):
        key = _FACILITY_WORDS.sub("", key)
    return key


def site_score(token: str, place: str) -> int:
    """How well a 3–4 letter hostname site token abbreviates a place name; 0 = it does not.
    5: one letter per word (LVU = Lippo Village Utara) · 3–4: letters taken in order from the
    consonants (SKL = Syiah Kuala, KKD = Kepanjenkidul) · 1–2: only in order somewhere in the name."""
    token = token.lower()
    words = place.split()
    if not words or not token.isalpha() or len(token) < 3:
        return 0
    if token.upper() in FUNCTION_TOKENS or token.upper() in _LITERAL_SITES:
        # NDC, NIX, IDC name a facility, not letters of a place: only the word itself counts.
        return 5 if token in words else 0
    initials = "".join(w[0] for w in words)
    if len(words) >= len(token) and initials.startswith(token):
        return 5
    joined = "".join(words)
    if joined[0] != token[0]:
        return 0
    letters = iter(joined)
    if not all(ch in letters for ch in token):
        return 0
    consonants = iter(re.sub(r"[aeiou]", "", joined))
    score = 3 if all(ch in consonants for ch in token[1:]) else 1
    if len(words) > 1 and token[1] == words[1][0]:
        score += 1  # the second letter opens the second word
    return score


_FACILITY = re.compile(r"^(olt|odc|sto|bbu)\b")


def _site_rank(pm: Placemark) -> tuple:
    """Among names for one site, a physical facility (NDC/MTI/Branch, then OLT/ODC/STO) beats a bare area name."""
    words = _words(pm.name)
    return (0 if _MAIN_SITE.match(words) else 1 if _FACILITY.match(words) else 2, pm.name)


def _site_confidence(score: int, distance_km: float) -> str:
    """Letter fit first, then distance: a good abbreviation 50 km out of town is only 'medium'."""
    level = 0 if score >= 3 else 1
    if distance_km > SITE_DOUBTFUL_KM:
        level = 2
    elif distance_km > SITE_SURE_KM:
        level = max(level, 1)
    return ("high", "medium", "low")[level]


def match_devices(devices: list[dict[str, Any]], placemarks: list[Placemark]) -> tuple[list[dict[str, Any]], int]:
    """Proposals for devices from site placemarks. Each device dict: id, hostname, pop, site, anchor
    ((lat, lon) of its POP or city centre, or None). Returns (proposals, ambiguous count)."""
    keyed = [(pm, _site_key(pm.name)) for pm in placemarks]
    proposals, ambiguous = [], 0
    for device in devices:
        site, anchor = device["site"], device["anchor"]
        if anchor is None or not site or not site.isalpha():
            continue
        scored = [(site_score(site, key), pm) for pm, key in keyed
                  if key and km_between(anchor, (pm.lat, pm.lon)) <= SITE_RADIUS_KM]
        scored = [(s, pm) for s, pm in scored if s]
        if not scored:
            continue
        best = max(s for s, _ in scored)
        top = sorted((pm for s, pm in scored if s == best), key=_site_rank)
        if any(km_between((top[0].lat, top[0].lon), (pm.lat, pm.lon)) > SAME_SITE_KM for pm in top[1:]):
            ambiguous += 1  # several different places fit the abbreviation: leave it to the user
            continue
        chosen = top[0]
        distance = km_between(anchor, (chosen.lat, chosen.lon))
        proposals.append({
            "device_id": device["id"], "hostname": device["hostname"], "pop": device["pop"], "site": site,
            "current_latitude": device.get("latitude"), "current_longitude": device.get("longitude"),
            "latitude": chosen.lat, "longitude": chosen.lon, "placemark": chosen.name, "source": chosen.source,
            "confidence": _site_confidence(best, distance), "score": best, "distance_km": round(distance, 1),
        })
    return proposals, ambiguous


def match_pops(pops: list[dict[str, Any]], placemarks: list[Placemark]) -> list[dict[str, Any]]:
    """Proposals for POPs; each pop dict needs id, code, name, city, area, latitude, longitude."""
    proposals = []
    for pop in pops:
        city, direction = _pop_city(pop)
        if not city:
            continue
        candidates = []
        rejected = 0
        for pm in placemarks:
            cand = _candidate(pm, city, direction)
            if cand is None:
                continue
            if in_area(pop["area"], pm.lat, pm.lon) is False:
                rejected += 1
                continue
            candidates.append(cand)
        if not candidates:
            continue
        best = min(candidates, key=lambda c: c.rank)
        proposals.append({
            "pop_id": pop["id"], "code": pop["code"], "name": pop["name"], "area": pop["area"], "city": city,
            "current_latitude": pop["latitude"], "current_longitude": pop["longitude"],
            "latitude": best.placemark.lat, "longitude": best.placemark.lon,
            "placemark": best.placemark.name, "source": best.placemark.source, "confidence": best.confidence,
            "candidates": len(candidates), "rejected_outside_area": rejected,
        })
    return proposals
