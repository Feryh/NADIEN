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

_JABO, _JBB, _JTG, _JTM = "Jabodetabek", "Jawa Barat & Banten", "Jawa Tengah & DIY", "Jawa Timur"
_SUM, _BNT, _KAL, _SUL, _MP = "Sumatera", "Bali & Nusa Tenggara", "Kalimantan", "Sulawesi", "Maluku & Papua"

# City dictionary: 3–4 letter POP/city abbreviation -> (city name as written in hostnames, area).
# There is no single official 3-letter list for Indonesian cities; this follows the abbreviations
# found in this network's hostnames and Observium locations, plus the usual telco/airport forms.
# Extend it here; a curated POP `region` always wins over it.
CITIES: dict[str, tuple[str, str]] = {
    # Jabodetabek
    "JKT": ("jakarta", _JABO), "BKS": ("bekasi", _JABO), "TNG": ("tangerang", _JABO), "BGR": ("bogor", _JABO),
    "DPK": ("depok", _JABO), "CKR": ("cikarang", _JABO), "CBR": ("cibubur", _JABO), "BSD": ("bsd", _JABO),
    "KWG": ("karawang", _JBB),
    # Jawa Barat & Banten
    "BDG": ("bandung", _JBB), "CRB": ("cirebon", _JBB), "SMI": ("sukabumi", _JBB), "TSK": ("tasikmalaya", _JBB),
    "TSM": ("tasikmalaya", _JBB), "GRT": ("garut", _JBB), "CMS": ("ciamis", _JBB), "IDM": ("indramayu", _JBB),
    "KRW": ("karawang", _JBB), "PWK": ("purwakarta", _JBB), "CJR": ("cianjur", _JBB), "MJL": ("majalengka", _JBB),
    "KNG": ("kuningan", _JBB), "SNG": ("subang", _JBB), "SRG": ("serang", _JBB),
    "CLG": ("cilegon", _JBB), "PDL": ("pandeglang", _JBB), "LBK": ("lebak", _JBB), "BJR": ("banjar", _JBB),
    # Jawa Tengah & DIY
    "SMG": ("semarang", _JTG), "SLO": ("solo", _JTG), "YGY": ("yogyakarta", _JTG), "YYK": ("yogyakarta", _JTG),
    "YGK": ("yogyakarta", _JTG), "BYL": ("boyolali", _JTG), "PBG": ("purbalingga", _JTG), "KDS": ("kudus", _JTG),
    "PWT": ("purwokerto", _JTG), "TGL": ("tegal", _JTG), "PKL": ("pekalongan", _JTG), "MGL": ("magelang", _JTG),
    "KLT": ("klaten", _JTG), "KLN": ("klaten", _JTG), "PTI": ("pati", _JTG), "JPR": ("jepara", _JTG),
    "JPA": ("jepara", _JTG), "CLP": ("cilacap", _JTG), "KBM": ("kebumen", _JTG), "SLT": ("salatiga", _JTG),
    "STG": ("salatiga", _JTG), "KDL": ("kendal", _JTG), "TMG": ("temanggung", _JTG), "DMK": ("demak", _JTG),
    "SGN": ("sragen", _JTG), "PWR": ("purworejo", _JTG), "PML": ("pemalang", _JTG), "BLA": ("blora", _JTG),
    "BRS": ("brebes", _JTG), "WNS": ("wonosobo", _JTG), "RBG": ("rembang", _JTG), "WNG": ("wonogiri", _JTG),
    "KRA": ("karanganyar", _JTG), "SKH": ("sukoharjo", _JTG), "BTG": ("batang", _JTG), "GBG": ("grobogan", _JTG),
    "BNR": ("banjarnegara", _JTG),
    # Jawa Timur
    "SBY": ("surabaya", _JTM), "MLG": ("malang", _JTM), "MJO": ("mojokerto", _JTM), "MJK": ("mojokerto", _JTM),
    "NJK": ("nganjuk", _JTM), "PRB": ("probolinggo", _JTM), "PBL": ("probolinggo", _JTM), "JBR": ("jember", _JTM),
    "JMR": ("jember", _JTM), "KDR": ("kediri", _JTM), "MDU": ("madiun", _JTM), "MAD": ("madiun", _JTM),
    "BWI": ("banyuwangi", _JTM), "BYW": ("banyuwangi", _JTM), "PSR": ("pasuruan", _JTM), "PSN": ("pasuruan", _JTM),
    "SDA": ("sidoarjo", _JTM), "GSK": ("gresik", _JTM), "GRK": ("gresik", _JTM), "BJN": ("bojonegoro", _JTM),
    "TBN": ("tuban", _JTM), "LMG": ("lamongan", _JTM), "BLT": ("blitar", _JTM), "TLG": ("tulungagung", _JTM),
    "PNG": ("ponorogo", _JTM), "JBG": ("jombang", _JTM), "NGW": ("ngawi", _JTM), "SIT": ("situbondo", _JTM),
    "LMJ": ("lumajang", _JTM), "BDW": ("bondowoso", _JTM), "MGT": ("magetan", _JTM), "PCT": ("pacitan", _JTM),
    "SPG": ("sampang", _JTM), "PMK": ("pamekasan", _JTM), "SMP": ("sumenep", _JTM),
    # Bali & Nusa Tenggara
    "DPS": ("denpasar", _BNT), "BDN": ("badung", _BNT), "MTM": ("mataram", _BNT), "MTR": ("mataram", _BNT),
    "KPG": ("kupang", _BNT), "KPN": ("kupang", _BNT), "BMA": ("bima", _BNT), "SBW": ("sumbawa", _BNT),
    "ATB": ("atambua", _BNT),
    # Sumatera
    "MDN": ("medan", _SUM), "PLG": ("palembang", _SUM), "PLB": ("palembang", _SUM), "PBR": ("pekanbaru", _SUM),
    "PKU": ("pekanbaru", _SUM), "BTM": ("batam", _SUM), "PDG": ("padang", _SUM), "PAD": ("padang", _SUM),
    "JMB": ("jambi", _SUM), "BDL": ("bandar lampung", _SUM), "LPG": ("lampung", _SUM), "BGL": ("bengkulu", _SUM),
    "BNA": ("banda aceh", _SUM), "PKP": ("pangkal pinang", _SUM), "DUM": ("dumai", _SUM),
    "PSP": ("padang sidempuan", _SUM), "PMS": ("pematang siantar", _SUM), "PBM": ("prabumulih", _SUM),
    "LLG": ("lubuk linggau", _SUM), "TJP": ("tanjung pinang", _SUM), "NTNA": ("natuna", _SUM),
    "LSM": ("lhokseumawe", _SUM), "LGS": ("langsa", _SUM), "LKT": ("langkat", _SUM), "MGR": ("manggar", _SUM),
    "TBT": ("tebing tinggi", _SUM), "LHT": ("lahat", _SUM),
    # Kalimantan
    "BPN": ("balikpapan", _KAL), "SMR": ("samarinda", _KAL), "PTK": ("pontianak", _KAL), "BJM": ("banjarmasin", _KAL),
    "BJB": ("banjarbaru", _KAL), "PKY": ("palangkaraya", _KAL), "PLK": ("palangkaraya", _KAL),
    "TRG": ("tenggarong", _KAL), "SKW": ("singkawang", _KAL), "PBU": ("pangkalan bun", _KAL),
    "TRK": ("tarakan", _KAL),
    # Sulawesi
    "MKS": ("makassar", _SUL), "MND": ("manado", _SUL), "KDI": ("kendari", _SUL), "PLU": ("palu", _SUL),
    "PAL": ("palu", _SUL), "GTO": ("gorontalo", _SUL), "MMJ": ("mamuju", _SUL), "BIT": ("bitung", _SUL),
    "PRE": ("pare pare", _SUL), "KTG": ("kotamobagu", _SUL), "PLW": ("polewali", _SUL),
    # Maluku & Papua
    "AMB": ("ambon", _MP), "JYP": ("jayapura", _MP), "SON": ("sorong", _MP), "TTE": ("ternate", _MP),
    "MKW": ("manokwari", _MP), "TIM": ("timika", _MP), "MRK": ("merauke", _MP),
    # Internasional
    "SIN": ("singapore", "Internasional"), "HKG": ("hong kong", "Internasional"),
}
# Codes that are also everyday tokens in hostnames (BSD the site, SIT/PAL/TIM words, model
# fragments): accepted as an explicit POP code, but never read from a hostname token.
_HOSTNAME_UNSAFE_CODES = {"BSD", "SIT", "PAL", "MAD", "TIM", "SON", "BLA", "PAD", "SMP", "KRA", "BIT", "PRE"}
# Other spellings of a city seen in hostnames/locations -> its code.
CITY_ALIASES: dict[str, str] = {
    "tasik": "TSK", "jogja": "YGY", "parepare": "PRE", "pangkalanbun": "PBU", "palangka raya": "PKY",
    "belitung timur": "MGR", "belu": "ATB",
}

_AREA_BY_CODE = {code: area for area, (_, codes) in AREAS.items() for code in codes}
_AREA_BY_CODE.update({code: area for code, (_, area) in CITIES.items()})
# City name -> preferred code: the first code listed for that city (YGY before YYK/YGK).
_CITY_CODE_BY_NAME: dict[str, str] = {}
for _code, (_name, _) in CITIES.items():
    _CITY_CODE_BY_NAME.setdefault(_name, _code)
_CITY_CODE_BY_NAME.update(CITY_ALIASES)

# Two-letter province abbreviations used in hostname tokens. Three-letter city
# abbreviations are resolved from _AREA_BY_CODE above; these aliases cover the
# common compact province/location forms used by network inventories.
_AREA_BY_SHORT_CODE = {
    "AC": "Sumatera", "SU": "Sumatera", "SB": "Sumatera", "RI": "Sumatera", "KR": "Sumatera",
    "JA": "Sumatera", "SS": "Sumatera", "BE": "Sumatera", "LA": "Sumatera", "BB": "Sumatera",
    "JK": "Jabodetabek", "JB": "Jawa Barat & Banten", "BT": "Jawa Barat & Banten",
    "JT": "Jawa Tengah & DIY", "YO": "Jawa Tengah & DIY", "JI": "Jawa Timur",
    "BA": "Bali & Nusa Tenggara", "NB": "Bali & Nusa Tenggara", "NT": "Bali & Nusa Tenggara",
    "KB": "Kalimantan", "KT": "Kalimantan", "KS": "Kalimantan", "KI": "Kalimantan", "KU": "Kalimantan",
    "SA": "Sulawesi", "ST": "Sulawesi", "SG": "Sulawesi", "SN": "Sulawesi", "SR": "Sulawesi", "GO": "Sulawesi",
    "MA": "Maluku & Papua", "MU": "Maluku & Papua", "PA": "Maluku & Papua", "PB": "Maluku & Papua",
}

# City / province words found in POP names (Observium location, e.g. "LAMPUNG - Tanjung Senang").
_AREA_KEYWORDS: dict[str, list[str]] = {
    "Internasional": ["singapore", "hong kong"],
    "Jabodetabek": ["jabodetabek", "dki jakarta", "jakarta", "bekasi", "tangerang", "bogor", "depok", "cibubur", "bsd", "kembangan", "cikarang"],
    "Jawa Barat & Banten": [
        "jawa barat", "jabar", "bandung", "subang", "tasikmalaya", "cirebon", "garut", "sukabumi", "cianjur", "karawang",
        "purwakarta", "indramayu", "majalengka", "kuningan", "ciamis", "banjar ", "serang", "cilegon", "banten",
        "pandeglang", "lebak", "sumedang",
    ],
    "Jawa Tengah & DIY": [
        "jawa tengah", "jateng", "diy", "yogyakarta", "jogja", "semarang", "surakarta", "solo", "kendal", "jepara", "klaten",
        "salatiga", "temanggung", "demak", "sragen", "purworejo", "pemalang", "blora", "brebes", "tegal",
        "pekalongan", "magelang", "kudus", "pati", "cilacap", "kebumen", "purwokerto", "banyumas", "boyolali",
        "wonosobo", "rembang", "grobogan", "wonogiri", "karanganyar", "sukoharjo", "batang", "kaliwungu",
    ],
    "Jawa Timur": [
        "jawa timur", "jatim", "surabaya", "malang", "gresik", "pasuruan", "madiun", "jombang", "banyuwangi", "ngawi",
        "probolinggo", "situbondo", "jember", "kediri", "sidoarjo", "mojokerto", "bojonegoro", "tuban",
        "lamongan", "blitar", "tulungagung", "ponorogo", "lumajang", "bondowoso", "nganjuk", "magetan", "pacitan",
        "trenggalek", "madura", "bangkalan", "sampang", "pamekasan", "sumenep",
    ],
    "Bali & Nusa Tenggara": ["bali", "denpasar", "badung", "mataram", "lombok", "ntb", "ntt", "kupang", "flores",
                             "sumbawa", "bima", "alor"],
    "Sumatera": [
        "sumatra", "sumatera", "sumut", "sumbar", "sumsel", "babel", "kepri", "lampung", "pekanbaru", "riau", "bengkulu", "padang", "palembang", "prabumulih",
        "lubuk linggau", "pematang siantar", "medan", "aceh", "jambi", "batam", "natuna", "bangka", "belitung",
        "dumai", "tanjung pinang", "kepri",
    ],
    "Kalimantan": ["kalimantan", "kalbar", "kalteng", "kalsel", "kaltim", "kaltara", "samarinda", "balikpapan", "pontianak", "singkawang", "banjarbaru", "banjarmasin",
                   "palangkaraya", "kutai", "tenggarong", "kotawaringin", "tarakan", "bontang"],
    "Sulawesi": ["sulawesi", "sulut", "sulteng", "sulsel", "sultra", "sulbar", "makassar", "palu", "manado", "kendari", "gorontalo", "mamuju"],
    "Maluku & Papua": ["maluku", "malut", "ambon", "ternate", "papua", "papua barat", "papua tengah", "papua pegunungan", "papua selatan", "jayapura", "sorong", "manokwari", "timika", "merauke"],
}
for _name, _area in {name: area for name, area in CITIES.values()}.items():
    if _name not in _AREA_KEYWORDS[_area]:
        _AREA_KEYWORDS[_area].append(_name)
for _alias, _code in CITY_ALIASES.items():
    _AREA_KEYWORDS[CITIES[_code][1]].append(_alias)
# Longest words first so "padang sidempuan" / "bandar lampung" win over "padang" / "lampung".
_KEYWORD_RE = sorted(
    ((re.compile(rf"(?<![a-z]){re.escape(word.strip())}(?![a-z])"), area, word.strip())
     for area, words in _AREA_KEYWORDS.items() for word in words),
    key=lambda item: -len(item[2]),
)
_CODE_TOKEN = re.compile(r"^([A-Z]{3,4})\d*$")  # JKT, JKT2, NTNA — not ATN910 / MA5800 / C300


def area_for_code(code: str) -> str | None:
    code = code.upper()
    return _AREA_BY_CODE.get(code) or _AREA_BY_CODE.get(code[:3])


def _words(text: str | None) -> str:
    """'PE1-ATN910-KEBUMEN' -> 'pe1 atn910 kebumen' so city names match as whole words."""
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower())


def area_for_name(name: str | None) -> str | None:
    """Area from a POP name/location such as 'Pekanbaru - SMT' or 'Kalimantan Barat - Singkawang'."""
    text = _words(name)
    return next((area for pattern, area, _ in _KEYWORD_RE if pattern.search(text)), None)


def city_code_for_hostname(hostname: str | None) -> str | None:
    """POP code for a city written out in a hostname (PE1-ATN910-KEBUMEN -> KBM), longest name wins."""
    text = _words(hostname)
    for pattern, _area, word in _KEYWORD_RE:
        if word in _CITY_CODE_BY_NAME and pattern.search(text):
            return _CITY_CODE_BY_NAME[word]
    return None


def _hostname_codes(hostname: str) -> list[str]:
    """City codes written as their own hostname token (JKT, JKT2, LPG) — model tokens never match."""
    codes = []
    for token in re.split(r"[^A-Z0-9]+", hostname.upper()):
        match = _CODE_TOKEN.match(token)
        if not match:
            continue
        code = match.group(1)
        for candidate in (code, code[:3]):
            if candidate in _AREA_BY_CODE and candidate not in _HOSTNAME_UNSAFE_CODES:
                codes.append(candidate)
                break
    return codes


def pop_code_for_hostname(hostname: str | None) -> str | None:
    """POP code a hostname points at explicitly: a city name, else a single city-code token."""
    if not hostname:
        return None
    by_name = city_code_for_hostname(hostname)
    if by_name:
        return by_name
    codes = set(_hostname_codes(hostname))
    return codes.pop() if len(codes) == 1 else None


def sibling_codes(code: str) -> list[str]:
    """All dictionary codes for the same city (SLT and STG are both Salatiga), the given one first."""
    code = code.upper()
    entry = CITIES.get(code)
    if entry is None:
        return [code]
    return [code] + [c for c, (name, _) in CITIES.items() if name == entry[0] and c != code]


def city_label(code: str) -> str | None:
    entry = CITIES.get(code.upper())
    return entry[0].title() if entry else None


def area_for_hostname(hostname: str | None) -> str | None:
    """Area written explicitly in a hostname: a city code token, a 2-letter province code (SNI ISO
    3166-2:ID, e.g. JT, JI) as its own token, or a city/province name. Only when they all agree."""
    if not hostname:
        return None
    candidates = {_AREA_BY_CODE[c] for c in _hostname_codes(hostname)}
    for token in re.split(r"[^A-Z0-9]+", hostname.upper()):
        if len(token) == 2 and token.isalpha() and token in _AREA_BY_SHORT_CODE:
            candidates.add(_AREA_BY_SHORT_CODE[token])
    explicit_name = area_for_name(hostname)
    if explicit_name:
        candidates.add(explicit_name)
    return next(iter(candidates)) if len(candidates) == 1 else None


def area_for_hostnames(hostnames: list[str], min_share: float = 0.8) -> str | None:
    """Area of a POP from its devices' hostnames: the area at least `min_share` of the
    hostnames that name a location agree on (a stray backhaul name doesn't block it)."""
    found = [area for hostname in hostnames if (area := area_for_hostname(hostname))]
    if not found:
        return None
    area, count = max(((a, found.count(a)) for a in set(found)), key=lambda x: x[1])
    return area if count / len(found) >= min_share else None


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
