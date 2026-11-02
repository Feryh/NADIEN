"""Grouping rules for reading a large network: Area > POP > Site > Device, and device categories.

- Area: the POP's `region` when set (curated), else a starting map from city/POP code.
  Codes are matched exactly, then by their first three letters (JKTS -> JKT, BKSB -> BKS).
  Ambiguous codes are deliberately left out — set the region on those POPs instead.
- Site: the hostname token right after the POP code (JKT-ABW-OLT1-HW -> ABW,
  OLT2-MA5603T-JKT-BSG -> BSG).
- Category: from the device role.
"""

import re

UNMAPPED_AREA = "Belum dipetakan"

# area -> (approx. longitude, latitude) for laying areas out west -> east, and its POP codes.
AREAS: dict[str, tuple[tuple[float, float], list[str]]] = {
    "Sumatera": ((101.0, 0.5), [
        "MDN", "PLG", "PBR", "BTM", "PDG", "JMB", "BDL", "BKL", "BNA", "PKP", "DUM", "PSP", "LSM", "TJP",
    ]),
    "Jabodetabek": ((106.8, -6.2), ["JKT", "BKS", "TNG", "BGR", "DPK", "CKR", "CBB"]),
    "Jawa Barat & Banten": ((107.6, -6.9), [
        "BDG", "CRB", "SMI", "TSM", "GRT", "CMS", "IDM", "KRW", "PWK", "CJR", "MJL", "KNG",
        "SRG", "CLG", "PDL", "LBK", "BJR", "CBN",
    ]),
    "Jawa Tengah & DIY": ((110.4, -7.2), [
        "SMG", "SLO", "YGY", "YYK", "BYL", "PBG", "KDS", "PWT", "TGL", "PKL", "MGL", "KLT", "PTI",
        "JPR", "CLP", "KBM", "SLT",
    ]),
    "Jawa Timur": ((112.7, -7.5), [
        "SBY", "MLG", "MJO", "NJK", "PRB", "JBR", "KDR", "MDU", "BWI", "PSR", "SDA", "GSK", "BJN",
        "TBN", "LMG", "BLT", "TLG", "PNG",
    ]),
    "Bali & Nusa Tenggara": ((116.0, -8.6), ["DPS", "MTR", "KPN", "BMA"]),
    "Kalimantan": ((115.5, -1.0), ["BPN", "SMD", "PTK", "BJM", "PKY", "TRK"]),
    "Sulawesi": ((121.0, -2.0), ["MKS", "MND", "KDI", "PLU", "GTO"]),
    "Maluku & Papua": ((133.0, -3.0), ["AMB", "JYP", "SON", "TTE", "MKW", "TIM", "MRK"]),
    "Internasional": ((98.0, 6.0), ["SIN", "HKG"]),
}
AREA_ORDER = list(AREAS) + [UNMAPPED_AREA]
_AREA_BY_CODE = {code: area for area, (_, codes) in AREAS.items() for code in codes}

# City / province words found in POP names (Observium location, e.g. "LAMPUNG - Tanjung Senang").
_AREA_KEYWORDS: dict[str, list[str]] = {
    "Internasional": ["singapore", "hong kong"],
    "Jabodetabek": ["jakarta", "bekasi", "tangerang", "bogor", "depok", "cibubur", "bsd", "kembangan", "cikarang"],
    "Jawa Barat & Banten": [
        "jawa barat", "bandung", "subang", "tasikmalaya", "cirebon", "garut", "sukabumi", "cianjur", "karawang",
        "purwakarta", "indramayu", "majalengka", "kuningan", "ciamis", "banjar ", "serang", "cilegon", "banten",
        "pandeglang", "lebak", "sumedang",
    ],
    "Jawa Tengah & DIY": [
        "jawa tengah", "yogyakarta", "jogja", "semarang", "surakarta", "solo", "kendal", "jepara", "klaten",
        "salatiga", "temanggung", "demak", "sragen", "purworejo", "pemalang", "blora", "brebes", "tegal",
        "pekalongan", "magelang", "kudus", "pati", "cilacap", "kebumen", "purwokerto", "banyumas", "boyolali",
        "wonosobo", "rembang", "grobogan", "wonogiri", "karanganyar", "sukoharjo", "batang", "kaliwungu",
    ],
    "Jawa Timur": [
        "jawa timur", "surabaya", "malang", "gresik", "pasuruan", "madiun", "jombang", "banyuwangi", "ngawi",
        "probolinggo", "situbondo", "jember", "kediri", "sidoarjo", "mojokerto", "bojonegoro", "tuban",
        "lamongan", "blitar", "tulungagung", "ponorogo", "lumajang", "bondowoso", "nganjuk", "magetan", "pacitan",
        "trenggalek", "madura", "bangkalan", "sampang", "pamekasan", "sumenep",
    ],
    "Bali & Nusa Tenggara": ["bali", "denpasar", "badung", "mataram", "lombok", "ntb", "ntt", "kupang", "flores",
                             "sumbawa", "bima", "alor"],
    "Sumatera": [
        "sumatra", "sumatera", "lampung", "pekanbaru", "riau", "bengkulu", "padang", "palembang", "prabumulih",
        "lubuk linggau", "pematang siantar", "medan", "aceh", "jambi", "batam", "natuna", "bangka", "belitung",
        "dumai", "tanjung pinang", "kepri",
    ],
    "Kalimantan": ["kalimantan", "samarinda", "balikpapan", "pontianak", "singkawang", "banjarbaru", "banjarmasin",
                   "palangkaraya", "kutai", "tenggarong", "kotawaringin", "tarakan", "bontang"],
    "Sulawesi": ["sulawesi", "makassar", "palu", "manado", "kendari", "gorontalo", "mamuju"],
    "Maluku & Papua": ["maluku", "ambon", "ternate", "papua", "jayapura", "sorong", "manokwari", "timika", "merauke"],
}
_KEYWORD_RE = [
    (re.compile(rf"(?<![a-z]){re.escape(word.strip())}(?![a-z])"), area)
    for area, words in _AREA_KEYWORDS.items() for word in words
]


def area_for_code(code: str) -> str | None:
    code = code.upper()
    return _AREA_BY_CODE.get(code) or _AREA_BY_CODE.get(code[:3])


def area_for_name(name: str | None) -> str | None:
    """Area from a POP name/location such as 'Pekanbaru - SMT' or 'Kalimantan Barat - Singkawang'."""
    text = (name or "").lower().replace("_", " ")
    return next((area for pattern, area in _KEYWORD_RE if pattern.search(text)), None)


def area_for_pop(code: str, region: str | None, name: str | None = None) -> str:
    """Curated region > POP code > city/province words in the POP name."""
    if (region or "").strip():
        return region.strip()
    if code.upper() == "UNASSIGNED":  # a parking lot for devices from anywhere, not a place
        return UNMAPPED_AREA
    return area_for_code(code) or area_for_name(name) or UNMAPPED_AREA


def area_position(area: str) -> tuple[float, float] | None:
    entry = AREAS.get(area)
    return entry[0] if entry else None


_TOKEN = re.compile(r"[-_.\s]+")


def site_for(hostname: str, pop_code: str) -> str:
    """Site code from the hostname: the token after the POP code, else '-'."""
    tokens = [t for t in _TOKEN.split(hostname.upper()) if t]
    pop = pop_code.upper()
    for i, token in enumerate(tokens[:-1]):
        if token == pop or token.rstrip("0123456789") == pop:
            nxt = tokens[i + 1]
            if nxt.isalnum() and not nxt.isdigit():
                return nxt
    return "-"


CATEGORIES: dict[str, list[str]] = {
    "router": ["p", "pe", "bng", "dci"],
    "metro": ["agg"],
    "olt": ["olt"],
    "access": ["access"],
}
CATEGORY_LABELS = {
    "router": "Router (P/PE/BNG/DCI)",
    "metro": "Metro / Aggregation",
    "olt": "OLT",
    "access": "Access / Switch",
    "other": "Lainnya",
}
_CATEGORY_BY_ROLE = {role: cat for cat, roles in CATEGORIES.items() for role in roles}


def category_for(role: str | None) -> str:
    return _CATEGORY_BY_ROLE.get((role or "").lower(), "other")


# Role tiers for layered device layouts (top to bottom).
ROLE_TIER = {"dci": 0, "p": 0, "pe": 1, "bng": 1, "agg": 2, "access": 3, "olt": 3}


# Standard port speeds; anything else (106G, 8G, 622M, SNMP overflow 4295G…) is "Non-standar":
# shaped/logical ports or bad counters, not a port class worth its own row.
STANDARD_SPEEDS: list[tuple[float, str]] = [
    (0.01, "10M"), (0.1, "100M"), (1, "1G"), (2.5, "2.5G"), (10, "10G"), (25, "25G"), (40, "40G"),
    (50, "50G"), (100, "100G"), (200, "200G"), (400, "400G"), (800, "800G"),
]
NON_STANDARD = "Non-standar"
_BUCKET_ORDER = {label: i for i, (_, label) in enumerate(STANDARD_SPEEDS)} | {NON_STANDARD: 98, "Unknown": 99}


def speed_bucket(speed_gbps: float | None) -> str:
    if speed_gbps is None or speed_gbps <= 0:
        return "Unknown"
    for nominal, label in STANDARD_SPEEDS:
        if abs(speed_gbps - nominal) <= nominal * 0.02:
            return label
    return NON_STANDARD


def bucket_sort_key(bucket: str) -> float:
    return _BUCKET_ORDER.get(bucket, 97)
