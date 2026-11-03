"""Coordinates for POPs from KML/KMZ placemarks (Google Earth exports of POP / OLT / ODC sites).

Placemarks are named after locations ("NDC Palembang", "MTI Kisaran", "OLT Telanaipura"), not after POP
codes, so a POP is matched through its city: the city of its code (BDG -> bandung, siblings included),
or a city word in the POP's name/location ("Bukit Tinggi - Sumatra Barat"). A candidate must lie inside
the POP's area box. Among several placemarks of one city the main site wins (NDC / MTI / Branch / Data
Centre), then the one named exactly after the city.
"""

import io
import re
import zipfile
from dataclasses import dataclass
from typing import Any
from xml.etree import ElementTree as ET

from .areas import CITIES, CITY_ALIASES, in_area

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
