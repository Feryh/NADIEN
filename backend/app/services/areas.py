"""Grouping rules for reading a large network: Area > POP > Site > Device, and device categories.

- Area: the POP's `region` when set (curated), else a starting map from city/POP code.
  Codes are matched exactly, then by their first three letters (JKTS -> JKT, BKSB -> BKS).
  Ambiguous codes are deliberately left out — set the region on those POPs instead.
- Site: the hostname token right after the POP code (JKT-ABW-OLT1-HW -> ABW,
  OLT2-MA5603T-JKT-BSG -> BSG).
- Category: from the device role.
"""

import re

from .rancid_locations import LOCATION_BY_HOSTNAME

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
    "Kalimantan": ((115.5, -1.0), ["BPN", "SMR", "PTK", "BJM", "PKY", "TRK"]),
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
    "BNR": ("banjarnegara", _JTG), "WSB": ("wonosobo", _JTG),
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
    # POPs whose Observium location names the city (Bukit Tinggi, Muara Enim, Payakumbuh, Sekayu Musi Banyuasin)
    "BKT": ("bukittinggi", _SUM), "MRE": ("muara enim", _SUM), "PYH": ("payakumbuh", _SUM),
    "MSB": ("musi banyuasin", _SUM),
    # Kalimantan
    "BPN": ("balikpapan", _KAL), "SMR": ("samarinda", _KAL), "SMD": ("sumedang", _JBB), "PTK": ("pontianak", _KAL), "BJM": ("banjarmasin", _KAL),
    "BJB": ("banjarbaru", _KAL), "PKY": ("palangkaraya", _KAL), "PLK": ("palangkaraya", _KAL),
    "TRG": ("tenggarong", _KAL), "SKW": ("singkawang", _KAL), "PBU": ("pangkalan bun", _KAL),
    "TRK": ("tarakan", _KAL),
    # Sulawesi
    "MKS": ("makassar", _SUL), "MND": ("manado", _SUL), "KDI": ("kendari", _SUL), "PLU": ("palu", _SUL),
    "PAL": ("palu", _SUL), "GTO": ("gorontalo", _SUL), "MMJ": ("mamuju", _SUL), "BIT": ("bitung", _SUL),
    "PRE": ("pare pare", _SUL), "KTG": ("kotamobagu", _SUL), "PLW": ("polewali", _SUL),
    "WTP": ("watampone", _SUL),
    # Maluku & Papua
    "AMB": ("ambon", _MP), "JYP": ("jayapura", _MP), "SON": ("sorong", _MP), "TTE": ("ternate", _MP),
    "MKW": ("manokwari", _MP), "TIM": ("timika", _MP), "MRK": ("merauke", _MP),
    # Internasional
    "SIN": ("singapore", "Internasional"), "HKG": ("hong kong", "Internasional"),
}
# POP and city codes confirmed by the supplied RANCID device/location list.
CITIES.update({
    # Jabodetabek and Banten
    "TGR": ("tangerang", _JABO), "TGRK": ("serang", _JBB),
    # Java
    "SMN": ("sleman", _JTG), "BTL": ("bantul", _JTG), "CEPU": ("cepu", _JTG),
    "CLCP": ("cilacap", _JTG), "GND": ("gandrungmangu", _JTG),
    "CBN": ("cirebon", _JBB),
    "JTM": ("madiun", _JTM), "KRS": ("probolinggo", _JTM),
    # Bali and Nusa Tenggara
    "ALR": ("alor", _BNT), "LBJ": ("labuan bajo", _BNT), "TGE": ("tange", _BNT),
    "RTG": ("ruteng", _BNT), "ENDE": ("ende", _BNT),
    # Sumatera and Riau Islands
    "LMP": ("bandar lampung", _SUM), "BGK": ("bangka", _SUM), "BTN": ("tanjung uban", _SUM),
    "MTK": ("matak", _SUM), "TRP": ("tarempa", _SUM), "ANTR": ("tarempa", _SUM),
    "TJB": ("tanjung balai karimun", _SUM), "PWG": ("perawang", _SUM), "BLS": ("bengkalis", _SUM),
    "SIAK": ("siak", _SUM), "UJT": ("ujung tanjung", _SUM),
    # Maluku and Papua
    "TMK": ("timika", _MP), "AGT": ("agats", _MP), "WTR": ("wetar", _MP),
    "SGP": ("sugapa", _MP), "TKR": ("tiakur", _MP), "TNH": ("tanah merah", _MP),
    "ENA": ("enarotali", _MP), "FEF": ("fef", _MP), "KYM": ("kenyam", _MP),
    "TIGI": ("tigi", _MP), "KTM": ("kotamulia", _MP), "KBG": ("karubaga", _MP),
    "TOM": ("tiom", _MP), "KBK": ("kobakma", _MP), "ARS": ("arso", _MP),
    "OSB": ("oksibil", _MP), "ELE": ("elelim", _MP), "KIG": ("kigamani", _MP),
    "ILA": ("ilaga", _MP), "KPI": ("kepi", _MP), "RAN": ("ransiki", _MP),
    "BIAK": ("biak", _MP), "SER": ("serui", _MP), "NUM": ("numfor", _MP),
    "BOT": ("botawa", _MP),
    # Additional locations identified in the inventory (including compact host tokens).
    "SMRG": ("semarang", _JTG), "LBP": ("lubuk pakam", _SUM), "PGP": ("pangkal pinang", _SUM),
    "KIS": ("kisaran", _SUM), "SRH": ("sei rampah", _SUM), "KLS": ("mandoge", _SUM),
    "RAP": ("rantau prapat", _SUM), "PWD": ("purwodadi", _JTG), "SOR": ("soreang", _JBB),
    "GMN": ("tebing tinggi", _SUM), "TTG": ("tebing tinggi", _SUM),
    "SPE": ("sape", _BNT),
    "DLK": ("daik lingga", _SUM),
    "HAU": ("haurgeulis", _JBB), "GMP": ("gempol", _JTM), "SNT": ("sentul", _JABO),
    "CIL": ("cileungsi", _JABO), "GKD": ("gunungkidul", _JTG), "WTS": ("wates", _JTG),
    "SGL": ("sungai liat", _SUM), "PAM": ("pamulang", _JABO), "KBY": ("kubu raya", _KAL),
    "BAJ": ("bajawa", _BNT), "DKAI": ("dekai", _MP), "TUL": ("tual", _MP),
    "NAB": ("nabire", _MP), "BTGD": ("batu gede", _BNT),
    # International
    "TLS": ("dili", "Internasional"),
})

# Codes that are also everyday tokens in hostnames (BSD the site, SIT/PAL/TIM words, model
# fragments): accepted as an explicit POP code, but never read from a hostname token.
_HOSTNAME_UNSAFE_CODES = {"BSD", "SIT", "PAL", "MAD", "TIM", "SON", "BLA", "PAD", "SMP", "KRA", "BIT", "PRE", "SNT"}
# LBP appears as a site token in hostnames, but isn't a curated POP code. Keep its
# city mapping for location/area lookup while leaving POP records for manual review.
_NON_POP_CITY_CODES = {"LBP"}
# Other spellings of a city seen in hostnames/locations -> its code.
CITY_ALIASES: dict[str, str] = {
    "tasik": "TSK", "jogja": "YGY", "parepare": "PRE", "pangkalanbun": "PBU", "palangka raya": "PKY",
    "belitung timur": "MGR", "belu": "ATB",
    "univ siliwangi": "TSK", "universitas siliwangi": "TSK", "unissula": "SMG",
    "universitas diponegoro": "SMG", "soeta": "TNG", "larangan": "TNG",
    "meruya": "JKT", "senayan": "JKT", "pramuka": "JKT", "taman ismail marzuki": "JKT",
    "boven digoel": "TNH", "batugede": "BTGD", "sumbawa besar": "SBW",
    "itc permata hijau": "JKT", "diskominfo dki": "JKT", "ratu prabu": "JKT",
    "upn veteran site 2": "DPK", "sekjen kementerian agama": "JKT",
    "kementerian ketenagakerjaan": "JKT", "universitas terbuka": "TNG",
    "universitas soedirman": "PWT", "tsel buaran": "JKT", "taruna bakti": "BDG",
    "ditlantas polda bali": "DPS", "ditlantas polda sumatera utara": "MDN",
    "wisma raharja": "JKT", "wijaya karya": "JKT", "yayasan al izhar": "JKT",
    "samratulangi": "MND", "metro tv joglo": "JKT", "mandiri rempoa": "TNG",
    "mabes tni au": "JKT", "kementrian komdigi": "JKT", "kementrian koperasi": "JKT",
    "kemenkeu": "JKT", "kemendesa": "JKT", "jati baru": "JKT",
    "gedung sapta pesona": "JKT", "formula e": "JKT", "diskominfo asahan": "KIS",
    "mten bulding": "JKT", "mten building": "JKT", "brantas abipraya": "JKT",
    "kawasan lintas biru digital": "CKR", "pager ngumbuk": "SDA",
    "mnc skyvision": "JKT", "midi utama indo": "TNG", "kominfo pdsi": "JKT",
    "pertamina ep": "JKT", "pusintek kemenkeu": "JKT",
    "pusintek kemakeu": "JKT", "gedung sapta pesono": "JKT",
    "yyg": "YGY", "unsoed idc duren": "JKT", "cibisnine": "JKT",
    "balai teknik bendungan": "JKT", "wisma millenia": "JKT", "bakti kominfo duren": "JKT",
    "transcosmos yogya": "YGY", "arkadia": "JKT", "kemhan": "JKT",
    "telkom karet": "JKT", "deltamas": "CKR", "daik lingga": "DLK",
    "menara danareksa": "JKT", "semen the east": "JKT", "mandiri grha mantap": "JKT",
    "gunung kidul": "GKD", "undip": "SMG", "boven dgl": "TNH", "kuburaya": "KBY",
    "gedung sapta pesona": "JKT", "ntb shape": "SPE",
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
_AREA_KEYWORDS["Jabodetabek"].append("kyaimaja")
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


# Hostname tokens that name a function, platform or company, never a place (CGW-NAP-BDG-BBU,
# VRR-MPLS-JKT-BSD, M6KS-BRAS-CGN-JKTT-PSR). They are skipped when looking for a POP code.
FUNCTION_TOKENS = {
    "NAP", "CGN", "NIX", "NDC", "CORE", "MPLS", "BRAS", "ISP", "CPE", "MGMT", "UTP", "OPT", "VMX", "VRR",
    "NEW", "MTI", "CDN", "DRC", "IDC", "ZTE", "HW", "FH", "DCI", "IXP", "OTT", "SPE", "IGW", "BNG", "OLT",
}


def is_city_code(code: str | None) -> bool:
    """A registered city code, or a 4-letter variant of one (JKTU / JKTS = Jakarta Utara / Selatan)."""
    code = (code or "").upper()
    return (bool(code) and code not in FUNCTION_TOKENS and code not in _NON_POP_CITY_CODES
            and (code in _AREA_BY_CODE or code[:3] in _AREA_BY_CODE))


def first_city_code(hostname: str | None) -> str | None:
    """POP code as written in the hostname: the first token, left to right, that is a registered city
    code (CGW-NAP-BDL-KDT -> BDL, M6KS-BRAS-CGN-JKTT-PSR -> JKTT). 4-letter variants are kept as
    written (JKTS stays JKTS). Codes that are also everyday hostname words are never read here."""
    for token in re.split(r"[^A-Z0-9]+", (hostname or "").upper()):
        match = _CODE_TOKEN.match(token)
        if not match:
            continue
        code = match.group(1)
        if is_city_code(code) and code not in _HOSTNAME_UNSAFE_CODES and code[:3] not in _HOSTNAME_UNSAFE_CODES:
            return code
    return None


def pop_code_for_hostname(hostname: str | None) -> str | None:
    """POP code from a cell-site name, first city token, or the supplied inventory location."""
    if not hostname:
        return None
    parts = csr_parts(hostname)
    if parts is not None:
        return parts[2]
    by_code = first_city_code(hostname)
    if by_code:
        return by_code
    # A directional district name isn't enough to infer the broader POP (for example,
    # NUMFOR-TIMUR); retain the area mapping but ask for a curated POP code.
    if re.search(r"(?<![a-z])numfor\s+timur(?![a-z])", _words(hostname)):
        return None
    location = LOCATION_BY_HOSTNAME.get(hostname.strip().upper())
    text = f"{hostname} {location}" if location else hostname
    return city_code_for_hostname(text)


def sibling_codes(code: str) -> list[str]:
    """All dictionary codes for the same city (SLT and STG are both Salatiga), the given one first."""
    code = code.upper()
    entry = CITIES.get(code)
    if entry is None:
        return [code]
    return [code] + [c for c, (name, _) in CITIES.items() if name == entry[0] and c != code]


def city_label(code: str) -> str | None:
    code = code.upper()
    entry = CITIES.get(code) or CITIES.get(code[:3])
    return entry[0].title() if entry else None


def area_for_hostname(hostname: str | None) -> str | None:
    """Area from a hostname: its first registered city code wins; names and province tokens are fallbacks."""
    if not hostname:
        return None
    csr_area = area_for_csr(hostname)
    if csr_area:
        return csr_area
    location = LOCATION_BY_HOSTNAME.get(hostname.strip().upper())
    location_area = area_for_name(location) if location else None
    if location_area:
        return location_area
    # Trunk names describe both ends of a circuit, so a city code on the left doesn't
    # establish the device's own area when another endpoint belongs to a different area.
    tokens = re.split(r"[^A-Z0-9]+", hostname.upper())
    is_trunk = any(token in {"TRUNK", "BACKHAUL", "TRANSIT", "INTERCONNECT"} for token in tokens)
    # The first registered city token is normally the POP; later tokens often name a remote customer site.
    primary_code = first_city_code(hostname)
    primary_area = area_for_code(primary_code) if primary_code else None
    if primary_area and not is_trunk:
        return primary_area
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


# Approximate city centres (lat, lon), keyed by the city names of CITIES. Only used to *estimate* where
# a POP without coordinates sits on the topology map (drawn dashed, never stored); real coordinates
# come from Observium or a KMZ import.
CITY_CENTERS: dict[str, tuple[float, float]] = {
    # Jabodetabek
    "jakarta": (-6.20, 106.85), "bekasi": (-6.24, 107.00), "tangerang": (-6.18, 106.63), "bogor": (-6.60, 106.80),
    "depok": (-6.40, 106.82), "cikarang": (-6.30, 107.15), "cibubur": (-6.37, 106.89), "bsd": (-6.30, 106.66),
    # Jawa Barat & Banten
    "karawang": (-6.30, 107.30), "bandung": (-6.91, 107.61), "cirebon": (-6.71, 108.56), "sukabumi": (-6.92, 106.93),
    "tasikmalaya": (-7.33, 108.22), "garut": (-7.22, 107.90), "ciamis": (-7.33, 108.35), "indramayu": (-6.33, 108.32),
    "purwakarta": (-6.56, 107.44), "cianjur": (-6.82, 107.14), "majalengka": (-6.84, 108.23), "kuningan": (-6.98, 108.48),
    "subang": (-6.57, 107.76), "serang": (-6.12, 106.15), "cilegon": (-6.00, 106.05), "pandeglang": (-6.31, 106.10),
    "lebak": (-6.36, 106.25), "banjar": (-7.37, 108.54),
    # Jawa Tengah & DIY
    "semarang": (-6.97, 110.42), "solo": (-7.57, 110.82), "yogyakarta": (-7.80, 110.37), "boyolali": (-7.53, 110.60),
    "purbalingga": (-7.39, 109.36), "kudus": (-6.80, 110.84), "purwokerto": (-7.42, 109.23), "tegal": (-6.87, 109.12),
    "pekalongan": (-6.89, 109.67), "magelang": (-7.48, 110.22), "klaten": (-7.71, 110.60), "pati": (-6.75, 111.04),
    "jepara": (-6.59, 110.67), "cilacap": (-7.73, 109.01), "kebumen": (-7.67, 109.65), "salatiga": (-7.33, 110.49),
    "kendal": (-6.92, 110.20), "temanggung": (-7.32, 110.17), "demak": (-6.89, 110.64), "sragen": (-7.43, 111.02),
    "purworejo": (-7.71, 110.01), "pemalang": (-6.89, 109.38), "blora": (-6.97, 111.42), "brebes": (-6.87, 109.04),
    "wonosobo": (-7.36, 109.90), "rembang": (-6.71, 111.34), "wonogiri": (-7.81, 110.92), "karanganyar": (-7.60, 110.95),
    "sukoharjo": (-7.68, 110.84), "batang": (-6.91, 109.73), "grobogan": (-7.08, 110.92), "banjarnegara": (-7.40, 109.69),
    # Jawa Timur
    "surabaya": (-7.26, 112.75), "malang": (-7.98, 112.63), "mojokerto": (-7.47, 112.43), "nganjuk": (-7.60, 111.90),
    "probolinggo": (-7.75, 113.22), "jember": (-8.17, 113.70), "kediri": (-7.82, 112.01), "madiun": (-7.63, 111.52),
    "banyuwangi": (-8.22, 114.37), "pasuruan": (-7.65, 112.91), "sidoarjo": (-7.45, 112.72), "gresik": (-7.16, 112.65),
    "bojonegoro": (-7.15, 111.88), "tuban": (-6.90, 112.05), "lamongan": (-7.12, 112.42), "blitar": (-8.10, 112.17),
    "tulungagung": (-8.07, 111.90), "ponorogo": (-7.87, 111.46), "jombang": (-7.55, 112.23), "ngawi": (-7.40, 111.45),
    "situbondo": (-7.71, 114.01), "lumajang": (-8.13, 113.22), "bondowoso": (-7.91, 113.82), "magetan": (-7.65, 111.33),
    "pacitan": (-8.20, 111.10), "sampang": (-7.19, 113.24), "pamekasan": (-7.16, 113.47), "sumenep": (-7.01, 113.86),
    # Bali & Nusa Tenggara
    "denpasar": (-8.65, 115.22), "badung": (-8.58, 115.18), "mataram": (-8.58, 116.12), "kupang": (-10.17, 123.61),
    "bima": (-8.46, 118.73), "sumbawa": (-8.49, 117.42), "atambua": (-9.11, 124.89),
    # Sumatera
    "medan": (3.59, 98.67), "palembang": (-2.98, 104.76), "pekanbaru": (0.51, 101.45), "batam": (1.13, 104.05),
    "padang": (-0.95, 100.35), "jambi": (-1.61, 103.61), "bandar lampung": (-5.43, 105.26), "lampung": (-5.43, 105.26),
    "bengkulu": (-3.80, 102.27), "banda aceh": (5.55, 95.32), "pangkal pinang": (-2.13, 106.11), "dumai": (1.67, 101.45),
    "padang sidempuan": (1.38, 99.27), "pematang siantar": (2.96, 99.06), "prabumulih": (-3.43, 104.23),
    "lubuk linggau": (-3.30, 102.86), "tanjung pinang": (0.92, 104.45), "natuna": (3.94, 108.38),
    "lhokseumawe": (5.18, 97.15), "langsa": (4.47, 97.97), "langkat": (3.75, 98.30), "manggar": (-2.87, 108.27),
    "tebing tinggi": (3.33, 99.16), "lahat": (-3.79, 103.54), "bukittinggi": (-0.30, 100.37),
    "muara enim": (-3.66, 103.77), "payakumbuh": (-0.22, 100.63), "musi banyuasin": (-2.88, 103.85),
    # Kalimantan
    "balikpapan": (-1.27, 116.83), "samarinda": (-0.50, 117.15), "pontianak": (-0.03, 109.33),
    "banjarmasin": (-3.32, 114.59), "banjarbaru": (-3.44, 114.83), "palangkaraya": (-2.21, 113.92),
    "tenggarong": (-0.42, 116.98), "singkawang": (0.91, 108.98), "pangkalan bun": (-2.68, 111.63), "tarakan": (3.30, 117.63),
    # Sulawesi
    "makassar": (-5.15, 119.43), "manado": (1.47, 124.84), "kendari": (-3.97, 122.51), "palu": (-0.90, 119.87),
    "gorontalo": (0.54, 123.06), "mamuju": (-2.68, 118.89), "bitung": (1.44, 125.19), "pare pare": (-4.01, 119.62),
    "kotamobagu": (0.73, 124.32), "polewali": (-3.41, 119.33), "watampone": (-4.54, 120.33),
    # Maluku & Papua
    "ambon": (-3.70, 128.18), "jayapura": (-2.53, 140.72), "sorong": (-0.88, 131.25), "ternate": (0.79, 127.38),
    "manokwari": (-0.86, 134.08), "timika": (-4.55, 136.89), "merauke": (-8.49, 140.40),
    # Internasional
    "singapore": (1.29, 103.85), "hong kong": (22.32, 114.17),
}


def city_center_for_code(code: str) -> tuple[float, float] | None:
    """Approximate centre of the city a POP code names (JKTS -> Jakarta), for map estimates only."""
    entry = CITIES.get(code.upper()) or CITIES.get(code.upper()[:3])
    return CITY_CENTERS.get(entry[0]) if entry else None


# Rough geographic boxes per area: (lat_min, lat_max, lon_min, lon_max). Used to reject a coordinate
# that names the right word in the wrong island ("Padang Galak" is in Bali, "ITC Kuningan" in Jakarta).
# Boxes may overlap at borders; Jabodetabek is cut out of Jawa Barat & Banten.
_BOX = tuple[float, float, float, float]
AREA_BOXES: dict[str, tuple[list[_BOX], list[_BOX]]] = {
    "Sumatera": ([(-6.3, 6.3, 94.5, 108.7)], []),
    "Jabodetabek": ([(-6.75, -5.85, 106.40, 107.20)], []),
    "Jawa Barat & Banten": ([(-7.9, -5.8, 105.0, 108.85)], [(-6.75, -5.85, 106.40, 107.20)]),
    "Jawa Tengah & DIY": ([(-8.3, -6.3, 108.75, 111.75)], []),
    "Jawa Timur": ([(-8.9, -6.6, 110.9, 116.3)], []),
    "Bali & Nusa Tenggara": ([(-11.2, -7.9, 114.4, 125.3)], []),
    "Kalimantan": ([(-4.3, 4.6, 108.7, 119.2)], []),
    "Sulawesi": ([(-6.6, 4.9, 118.7, 125.9)], []),
    "Maluku & Papua": ([(-9.6, 3.2, 124.0, 141.3)], []),
}


def in_area(area: str, lat: float, lon: float) -> bool | None:
    """True/False when the area has a box; None when it cannot be checked (unmapped, international)."""
    boxes = AREA_BOXES.get(area)
    if boxes is None:
        return None
    include, exclude = boxes

    def inside(box: _BOX) -> bool:
        return box[0] <= lat <= box[1] and box[2] <= lon <= box[3]

    return any(inside(b) for b in include) and not any(inside(b) for b in exclude)


def area_position(area: str) -> tuple[float, float] | None:
    entry = AREAS.get(area)
    return entry[0] if entry else None


_TOKEN = re.compile(r"[-_.\s]+")

# Cell-site router naming: REGION-PROVINCE-CITY-SITEID-ROLE-VENDOR, e.g. BAL-BA-BLI-0004-ME1-ZTE,
# SUL-SN-SGM-0078-ME1-ZTE. The region is a 3-letter island group, the province the 2-letter code.
_CSR_HOSTNAME = re.compile(r"^(SUM|SUL|KAL|BAL|NUS|JAW|JAV|PAP|MAL|MLK)-([A-Z]{2})-([A-Z]{3,4})-(\d{3,6})(?:-|$)")
_AREA_BY_REGION_TOKEN = {
    "SUM": "Sumatera", "SUL": "Sulawesi", "KAL": "Kalimantan", "BAL": "Bali & Nusa Tenggara",
    "NUS": "Bali & Nusa Tenggara", "PAP": "Maluku & Papua", "MAL": "Maluku & Papua", "MLK": "Maluku & Papua",
    "NTB": "Bali & Nusa Tenggara", "NTT": "Bali & Nusa Tenggara",
}


def csr_parts(hostname: str | None) -> tuple[str, str, str, str] | None:
    """(region, province, city, site id) for a cell-site style hostname, else None."""
    match = _CSR_HOSTNAME.match((hostname or "").strip().upper())
    return match.groups() if match else None  # type: ignore[return-value]


def area_for_csr(hostname: str | None) -> str | None:
    parts = csr_parts(hostname)
    if parts is None:
        return None
    region, province, _, _ = parts
    return _AREA_BY_SHORT_CODE.get(province) or _AREA_BY_REGION_TOKEN.get(region)


def site_for(hostname: str, pop_code: str) -> str:
    """Site code from the hostname: the token after the POP code, else '-' (cell sites: the site id)."""
    parts = csr_parts(hostname)
    if parts is not None:
        return parts[3]
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
    "csr": ["csr"],
    "olt": ["olt"],
    "access": ["access"],
}
CATEGORY_LABELS = {
    "router": "Router (P/PE/BNG/DCI)",
    "metro": "Metro / Aggregation",
    "csr": "CSR (Cell Site Router)",
    "olt": "OLT",
    "access": "Access / Switch",
    "other": "Lainnya",
}
CATEGORY_ORDER = list(CATEGORY_LABELS)
CATEGORY_PATTERN = "^(" + "|".join(CATEGORY_ORDER) + ")$"  # for Query(pattern=...)
_CATEGORY_BY_ROLE = {role: cat for cat, roles in CATEGORIES.items() for role in roles}


def category_for(role: str | None) -> str:
    return _CATEGORY_BY_ROLE.get((role or "").lower(), "other")


# Role tiers for layered device layouts (top to bottom).
ROLE_TIER = {"dci": 0, "p": 0, "pe": 1, "bng": 1, "agg": 2, "csr": 3, "access": 3, "olt": 3}


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
