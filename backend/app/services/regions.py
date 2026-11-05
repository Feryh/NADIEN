"""Province and regency (kabupaten/kota) of a POP, so the topology can drill Area > Provinsi > Kab/Kota > POP.

The city a POP serves comes from its curated `city`, else a city named in its `name` (Observium location),
else its code (`CITIES` in `areas.py`). That city is then looked up here. Cities that are only a district
(Purwokerto, Cikarang, Soreang) belong to the regency that governs them; a city that is both a kota and a
kabupaten (Bekasi, Bogor, Malang) is taken as the kota, where the POP almost always sits.

A candidate whose province lies outside the POP's area is skipped: curated cities are sometimes swapped
between look-alike codes (PWK "Purwokerto", PWT "Purwakarta"), and the area is what the network team
maintains. With no usable city, the province alone may still come from a province word in the code/name
(NTT, BALI) or from cell-site hostnames (SUM-LA-GNS-0006-ME1 -> Lampung).
"""

import re
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass

from .areas import AREAS, CITIES, city_code_for_hostname, csr_parts

UNMAPPED_PROVINCE = "Provinsi belum dipetakan"
UNMAPPED_REGENCY = "Kab/Kota belum dipetakan"

_ACEH, _SU, _SB, _RI, _KR = "Aceh", "Sumatera Utara", "Sumatera Barat", "Riau", "Kepulauan Riau"
_JA, _SS, _BB, _BE, _LA = "Jambi", "Sumatera Selatan", "Kepulauan Bangka Belitung", "Bengkulu", "Lampung"
_JK, _JB, _BT, _JT, _YO, _JI = "DKI Jakarta", "Jawa Barat", "Banten", "Jawa Tengah", "DI Yogyakarta", "Jawa Timur"
_BA, _NB, _NT = "Bali", "Nusa Tenggara Barat", "Nusa Tenggara Timur"
_KB, _KT, _KS, _KI, _KU = "Kalimantan Barat", "Kalimantan Tengah", "Kalimantan Selatan", "Kalimantan Timur", "Kalimantan Utara"
_SA, _GO, _ST, _SR, _SN, _SG = ("Sulawesi Utara", "Gorontalo", "Sulawesi Tengah", "Sulawesi Barat", "Sulawesi Selatan",
                                "Sulawesi Tenggara")
_MA, _MU = "Maluku", "Maluku Utara"
_PA, _PB, _PBD, _PT, _PP, _PS = ("Papua", "Papua Barat", "Papua Barat Daya", "Papua Tengah", "Papua Pegunungan",
                                 "Papua Selatan")

# City name (as written in CITIES) -> (regency, province). Abroad the country stands in for the province.
REGENCY_OF_CITY: dict[str, tuple[str, str]] = {
    # DKI Jakarta and the Jabodetabek ring
    "jakarta": ("Jakarta", _JK), "cibubur": ("Jakarta Timur", _JK),
    "bekasi": ("Kota Bekasi", _JB), "cikarang": ("Kab. Bekasi", _JB), "bogor": ("Kota Bogor", _JB),
    "sentul": ("Kab. Bogor", _JB), "cileungsi": ("Kab. Bogor", _JB), "depok": ("Kota Depok", _JB),
    "tangerang": ("Kota Tangerang", _BT), "bsd": ("Kota Tangerang Selatan", _BT),
    "pamulang": ("Kota Tangerang Selatan", _BT),
    # Jawa Barat
    "bandung": ("Kota Bandung", _JB), "soreang": ("Kab. Bandung", _JB), "karawang": ("Kab. Karawang", _JB),
    "cirebon": ("Kota Cirebon", _JB), "sukabumi": ("Kota Sukabumi", _JB), "tasikmalaya": ("Kota Tasikmalaya", _JB),
    "garut": ("Kab. Garut", _JB), "ciamis": ("Kab. Ciamis", _JB), "indramayu": ("Kab. Indramayu", _JB),
    "haurgeulis": ("Kab. Indramayu", _JB), "purwakarta": ("Kab. Purwakarta", _JB), "cianjur": ("Kab. Cianjur", _JB),
    "majalengka": ("Kab. Majalengka", _JB), "kuningan": ("Kab. Kuningan", _JB), "subang": ("Kab. Subang", _JB),
    "banjar": ("Kota Banjar", _JB), "sumedang": ("Kab. Sumedang", _JB),
    # Banten
    "serang": ("Kota Serang", _BT), "cilegon": ("Kota Cilegon", _BT), "pandeglang": ("Kab. Pandeglang", _BT),
    "lebak": ("Kab. Lebak", _BT),
    # Jawa Tengah
    "semarang": ("Kota Semarang", _JT), "solo": ("Kota Surakarta", _JT), "boyolali": ("Kab. Boyolali", _JT),
    "purbalingga": ("Kab. Purbalingga", _JT), "kudus": ("Kab. Kudus", _JT), "purwokerto": ("Kab. Banyumas", _JT),
    "tegal": ("Kota Tegal", _JT), "pekalongan": ("Kota Pekalongan", _JT), "magelang": ("Kota Magelang", _JT),
    "klaten": ("Kab. Klaten", _JT), "pati": ("Kab. Pati", _JT), "jepara": ("Kab. Jepara", _JT),
    "cilacap": ("Kab. Cilacap", _JT), "gandrungmangu": ("Kab. Cilacap", _JT), "kebumen": ("Kab. Kebumen", _JT),
    "salatiga": ("Kota Salatiga", _JT), "kendal": ("Kab. Kendal", _JT), "temanggung": ("Kab. Temanggung", _JT),
    "demak": ("Kab. Demak", _JT), "sragen": ("Kab. Sragen", _JT), "purworejo": ("Kab. Purworejo", _JT),
    "pemalang": ("Kab. Pemalang", _JT), "blora": ("Kab. Blora", _JT), "cepu": ("Kab. Blora", _JT),
    "brebes": ("Kab. Brebes", _JT), "wonosobo": ("Kab. Wonosobo", _JT), "rembang": ("Kab. Rembang", _JT),
    "wonogiri": ("Kab. Wonogiri", _JT), "karanganyar": ("Kab. Karanganyar", _JT),
    "sukoharjo": ("Kab. Sukoharjo", _JT), "batang": ("Kab. Batang", _JT), "grobogan": ("Kab. Grobogan", _JT),
    "purwodadi": ("Kab. Grobogan", _JT), "banjarnegara": ("Kab. Banjarnegara", _JT),
    # DI Yogyakarta
    "yogyakarta": ("Kota Yogyakarta", _YO), "sleman": ("Kab. Sleman", _YO), "bantul": ("Kab. Bantul", _YO),
    "gunungkidul": ("Kab. Gunungkidul", _YO), "wates": ("Kab. Kulon Progo", _YO),
    # Jawa Timur
    "surabaya": ("Kota Surabaya", _JI), "malang": ("Kota Malang", _JI), "mojokerto": ("Kota Mojokerto", _JI),
    "nganjuk": ("Kab. Nganjuk", _JI), "probolinggo": ("Kota Probolinggo", _JI), "jember": ("Kab. Jember", _JI),
    "kediri": ("Kota Kediri", _JI), "madiun": ("Kota Madiun", _JI), "banyuwangi": ("Kab. Banyuwangi", _JI),
    "pasuruan": ("Kota Pasuruan", _JI), "gempol": ("Kab. Pasuruan", _JI), "sidoarjo": ("Kab. Sidoarjo", _JI),
    "gresik": ("Kab. Gresik", _JI), "bojonegoro": ("Kab. Bojonegoro", _JI), "tuban": ("Kab. Tuban", _JI),
    "lamongan": ("Kab. Lamongan", _JI), "blitar": ("Kota Blitar", _JI), "tulungagung": ("Kab. Tulungagung", _JI),
    "ponorogo": ("Kab. Ponorogo", _JI), "jombang": ("Kab. Jombang", _JI), "ngawi": ("Kab. Ngawi", _JI),
    "situbondo": ("Kab. Situbondo", _JI), "lumajang": ("Kab. Lumajang", _JI), "bondowoso": ("Kab. Bondowoso", _JI),
    "magetan": ("Kab. Magetan", _JI), "pacitan": ("Kab. Pacitan", _JI), "sampang": ("Kab. Sampang", _JI),
    "pamekasan": ("Kab. Pamekasan", _JI), "sumenep": ("Kab. Sumenep", _JI),
    # Bali, Nusa Tenggara
    "denpasar": ("Kota Denpasar", _BA), "badung": ("Kab. Badung", _BA), "jembrana": ("Kab. Jembrana", _BA),
    "mataram": ("Kota Mataram", _NB), "bima": ("Kota Bima", _NB), "sape": ("Kab. Bima", _NB),
    "sumbawa": ("Kab. Sumbawa", _NB),
    "kupang": ("Kota Kupang", _NT), "atambua": ("Kab. Belu", _NT), "alor": ("Kab. Alor", _NT),
    "labuan bajo": ("Kab. Manggarai Barat", _NT), "ruteng": ("Kab. Manggarai", _NT), "ende": ("Kab. Ende", _NT),
    "bajawa": ("Kab. Ngada", _NT),
    # Sumatera
    "banda aceh": ("Kota Banda Aceh", _ACEH), "lhokseumawe": ("Kota Lhokseumawe", _ACEH),
    "langsa": ("Kota Langsa", _ACEH),
    "medan": ("Kota Medan", _SU), "padang sidempuan": ("Kota Padangsidimpuan", _SU),
    "pematang siantar": ("Kota Pematangsiantar", _SU), "langkat": ("Kab. Langkat", _SU),
    "tebing tinggi": ("Kota Tebing Tinggi", _SU), "lubuk pakam": ("Kab. Deli Serdang", _SU),
    "kisaran": ("Kab. Asahan", _SU), "mandoge": ("Kab. Asahan", _SU), "sei rampah": ("Kab. Serdang Bedagai", _SU),
    "rantau prapat": ("Kab. Labuhanbatu", _SU),
    "padang": ("Kota Padang", _SB), "bukittinggi": ("Kota Bukittinggi", _SB), "payakumbuh": ("Kota Payakumbuh", _SB),
    "pekanbaru": ("Kota Pekanbaru", _RI), "dumai": ("Kota Dumai", _RI), "perawang": ("Kab. Siak", _RI),
    "siak": ("Kab. Siak", _RI), "bengkalis": ("Kab. Bengkalis", _RI), "ujung tanjung": ("Kab. Rokan Hilir", _RI),
    "batam": ("Kota Batam", _KR), "tanjung pinang": ("Kota Tanjungpinang", _KR), "natuna": ("Kab. Natuna", _KR),
    "tanjung uban": ("Kab. Bintan", _KR), "matak": ("Kab. Kepulauan Anambas", _KR),
    "tarempa": ("Kab. Kepulauan Anambas", _KR), "tanjung balai karimun": ("Kab. Karimun", _KR),
    "daik lingga": ("Kab. Lingga", _KR),
    "jambi": ("Kota Jambi", _JA),
    "palembang": ("Kota Palembang", _SS), "prabumulih": ("Kota Prabumulih", _SS),
    "lubuk linggau": ("Kota Lubuklinggau", _SS), "lahat": ("Kab. Lahat", _SS), "muara enim": ("Kab. Muara Enim", _SS),
    "musi banyuasin": ("Kab. Musi Banyuasin", _SS),
    "pangkal pinang": ("Kota Pangkalpinang", _BB), "bangka": ("Kab. Bangka", _BB), "sungai liat": ("Kab. Bangka", _BB),
    "manggar": ("Kab. Belitung Timur", _BB),
    "bengkulu": ("Kota Bengkulu", _BE),
    "bandar lampung": ("Kota Bandar Lampung", _LA), "lampung": ("Kota Bandar Lampung", _LA),
    # Kalimantan
    "pontianak": ("Kota Pontianak", _KB), "singkawang": ("Kota Singkawang", _KB), "kubu raya": ("Kab. Kubu Raya", _KB),
    "palangkaraya": ("Kota Palangka Raya", _KT), "pangkalan bun": ("Kab. Kotawaringin Barat", _KT),
    "banjarmasin": ("Kota Banjarmasin", _KS), "banjarbaru": ("Kota Banjarbaru", _KS),
    "balikpapan": ("Kota Balikpapan", _KI), "samarinda": ("Kota Samarinda", _KI),
    "tenggarong": ("Kab. Kutai Kartanegara", _KI),
    "tarakan": ("Kota Tarakan", _KU),
    # Sulawesi
    "manado": ("Kota Manado", _SA), "bitung": ("Kota Bitung", _SA), "kotamobagu": ("Kota Kotamobagu", _SA),
    "gorontalo": ("Kota Gorontalo", _GO),
    "palu": ("Kota Palu", _ST),
    "mamuju": ("Kab. Mamuju", _SR), "polewali": ("Kab. Polewali Mandar", _SR),
    "makassar": ("Kota Makassar", _SN), "pare pare": ("Kota Parepare", _SN), "watampone": ("Kab. Bone", _SN),
    "kendari": ("Kota Kendari", _SG),
    # Maluku
    "ambon": ("Kota Ambon", _MA), "tual": ("Kota Tual", _MA), "tiakur": ("Kab. Maluku Barat Daya", _MA),
    "wetar": ("Kab. Maluku Barat Daya", _MA),
    "ternate": ("Kota Ternate", _MU),
    # Papua (since the 2022 split into six provinces)
    "jayapura": ("Kota Jayapura", _PA), "arso": ("Kab. Keerom", _PA), "biak": ("Kab. Biak Numfor", _PA),
    "numfor": ("Kab. Biak Numfor", _PA), "serui": ("Kab. Kepulauan Yapen", _PA), "botawa": ("Kab. Waropen", _PA),
    "manokwari": ("Kab. Manokwari", _PB), "ransiki": ("Kab. Manokwari Selatan", _PB),
    "sorong": ("Kota Sorong", _PBD), "fef": ("Kab. Tambrauw", _PBD),
    "timika": ("Kab. Mimika", _PT), "nabire": ("Kab. Nabire", _PT), "enarotali": ("Kab. Paniai", _PT),
    "sugapa": ("Kab. Intan Jaya", _PT), "ilaga": ("Kab. Puncak", _PT), "kotamulia": ("Kab. Puncak Jaya", _PT),
    "tigi": ("Kab. Deiyai", _PT),
    "karubaga": ("Kab. Tolikara", _PP), "tiom": ("Kab. Lanny Jaya", _PP), "kobakma": ("Kab. Mamberamo Tengah", _PP),
    "oksibil": ("Kab. Pegunungan Bintang", _PP), "elelim": ("Kab. Yalimo", _PP), "kenyam": ("Kab. Nduga", _PP),
    "dekai": ("Kab. Yahukimo", _PP),
    "merauke": ("Kab. Merauke", _PS), "agats": ("Kab. Asmat", _PS), "tanah merah": ("Kab. Boven Digoel", _PS),
    "kepi": ("Kab. Mappi", _PS),
    # Abroad
    "singapore": ("Singapura", "Singapura"), "hong kong": ("Hong Kong", "Hong Kong"),
    "dili": ("Dili", "Timor-Leste"), "batu gede": ("Bobonaro", "Timor-Leste"),
    # Curated city names seen on POPs whose code is not in CITIES
    "bangkalan": ("Kab. Bangkalan", _JI), "kaban jahe": ("Kab. Karo", _SU), "kabanjahe": ("Kab. Karo", _SU),
    "waingapu": ("Kab. Sumba Timur", _NT), "wamena": ("Kab. Jayawijaya", _PP), "aru": ("Kab. Kepulauan Aru", _MA),
    "dobo": ("Kab. Kepulauan Aru", _MA), "bontang": ("Kota Bontang", _KI), "kolaka": ("Kab. Kolaka", _SG),
}

_SUMATERA = {_ACEH, _SU, _SB, _RI, _KR, _JA, _SS, _BB, _BE, _LA}
_KALIMANTAN = {_KB, _KT, _KS, _KI, _KU}
_SULAWESI = {_SA, _GO, _ST, _SR, _SN, _SG}
_MALUKU_PAPUA = {_MA, _MU, _PA, _PB, _PBD, _PT, _PP, _PS}
# Areas (as in areas.AREAS) a province may sit in. Jabodetabek takes the Jakarta ring of Jawa Barat and Banten.
_AREAS_OF_PROVINCE: dict[str, set[str]] = {
    _JK: {"Jabodetabek"}, _JB: {"Jabodetabek", "Jawa Barat & Banten"}, _BT: {"Jabodetabek", "Jawa Barat & Banten"},
    _JT: {"Jawa Tengah & DIY"}, _YO: {"Jawa Tengah & DIY"}, _JI: {"Jawa Timur"},
    _BA: {"Bali & Nusa Tenggara"}, _NB: {"Bali & Nusa Tenggara"}, _NT: {"Bali & Nusa Tenggara"},
    "Timor-Leste": {"Bali & Nusa Tenggara", "Internasional"}, "Singapura": {"Internasional"},
    "Hong Kong": {"Internasional"},
    **{p: {"Sumatera"} for p in _SUMATERA}, **{p: {"Kalimantan"} for p in _KALIMANTAN},
    **{p: {"Sulawesi"} for p in _SULAWESI}, **{p: {"Maluku & Papua"} for p in _MALUKU_PAPUA},
}

# Two-letter province codes of cell-site hostnames (SUL-SN-SGM-0066-ME1 -> Sulawesi Selatan).
_PROVINCE_BY_CSR_CODE = {
    "AC": _ACEH, "SU": _SU, "SB": _SB, "RI": _RI, "KR": _KR, "JA": _JA, "SS": _SS, "BE": _BE, "LA": _LA, "BB": _BB,
    "JK": _JK, "JB": _JB, "BT": _BT, "JT": _JT, "YO": _YO, "JI": _JI, "BA": _BA, "NB": _NB, "NT": _NT,
    "KB": _KB, "KT": _KT, "KS": _KS, "KI": _KI, "KU": _KU, "SA": _SA, "ST": _ST, "SG": _SG, "SN": _SN, "SR": _SR,
    "GO": _GO, "MA": _MA, "MU": _MU, "PA": _PA, "PB": _PB,
}

# Town codes of cell-site hostnames, keyed by (province code, town code) since the 3-letter town codes repeat
# across provinces: -> (town, regency). Only codes whose town is certain; others keep just their province.
_CSR_TOWNS: dict[tuple[str, str], tuple[str, str]] = {
    ("BA", "GIN"): ("Gianyar", "Kab. Gianyar"), ("BA", "NGA"): ("Negara", "Kab. Jembrana"),
    ("BA", "SGR"): ("Singaraja", "Kab. Buleleng"), ("BA", "SRP"): ("Semarapura", "Kab. Klungkung"),
    ("BA", "TAB"): ("Tabanan", "Kab. Tabanan"), ("BA", "BLI"): ("Bangli", "Kab. Bangli"),
    ("BA", "MGW"): ("Mengwi", "Kab. Badung"),
    ("NB", "GRG"): ("Gerung", "Kab. Lombok Barat"), ("NB", "PYA"): ("Praya", "Kab. Lombok Tengah"),
    ("NB", "SEL"): ("Selong", "Kab. Lombok Timur"), ("NB", "TJN"): ("Tanjung", "Kab. Lombok Utara"),
    ("KB", "MPW"): ("Mempawah", "Kab. Mempawah"),
    ("KI", "BPP"): ("Balikpapan", "Kota Balikpapan"), ("KI", "PNJ"): ("Penajam", "Kab. Penajam Paser Utara"),
    ("KI", "BON"): ("Bontang", "Kota Bontang"),
    ("KS", "MRH"): ("Marabahan", "Kab. Barito Kuala"), ("KS", "MTP"): ("Martapura", "Kab. Banjar"),
    ("SA", "ARM"): ("Airmadidi", "Kab. Minahasa Utara"), ("SA", "TMH"): ("Tomohon", "Kota Tomohon"),
    ("SA", "TNN"): ("Tondano", "Kab. Minahasa"),
    ("SN", "MRS"): ("Maros", "Kab. Maros"), ("SN", "SGM"): ("Sungguminasa", "Kab. Gowa"),
    ("SN", "TKA"): ("Takalar", "Kab. Takalar"),
    ("AC", "BIR"): ("Bireuen", "Kab. Bireuen"), ("BE", "TAS"): ("Tais", "Kab. Seluma"),
    ("JA", "MBN"): ("Muara Bulian", "Kab. Batanghari"), ("JA", "SNT"): ("Sengeti", "Kab. Muaro Jambi"),
    ("LA", "GDT"): ("Gedong Tataan", "Kab. Pesawaran"), ("LA", "KLA"): ("Kalianda", "Kab. Lampung Selatan"),
    ("LA", "KTB"): ("Kotabumi", "Kab. Lampung Utara"), ("LA", "MET"): ("Metro", "Kota Metro"),
    ("LA", "PRW"): ("Pringsewu", "Kab. Pringsewu"), ("LA", "SDN"): ("Sukadana", "Kab. Lampung Timur"),
    ("LA", "GNS"): ("Gunung Sugih", "Kab. Lampung Tengah"),
    ("RI", "BKN"): ("Bangkinang", "Kab. Kampar"), ("RI", "SAK"): ("Siak", "Kab. Siak"),
    ("SB", "PDP"): ("Padang Pariaman", "Kab. Padang Pariaman"), ("SB", "PMN"): ("Pariaman", "Kota Pariaman"),
    ("SB", "SLK"): ("Solok", "Kota Solok"), ("SB", "SWL"): ("Sawahlunto", "Kota Sawahlunto"),
    ("SB", "ARS"): ("Arosuka", "Kab. Solok"),
    ("SS", "IDL"): ("Indralaya", "Kab. Ogan Ilir"), ("SS", "SKY"): ("Sekayu", "Kab. Musi Banyuasin"),
    ("SU", "BNJ"): ("Binjai", "Kota Binjai"),
}

# Province words used as a POP code or name (POP "NTT", "BALI").
_PROVINCE_ALIASES = {
    "ntt": _NT, "ntb": _NB, "bali": _BA, "dki": _JK, "jabar": _JB, "jateng": _JT, "jatim": _JI, "diy": _YO,
    "sumut": _SU, "sumbar": _SB, "sumsel": _SS, "kepri": _KR, "babel": _BB, "kalbar": _KB, "kalteng": _KT,
    "kalsel": _KS, "kaltim": _KI, "kaltara": _KU, "sulut": _SA, "sulteng": _ST, "sultra": _SG, "sulsel": _SN,
    "sulbar": _SR, "malut": _MU,
}

# Jakarta POP codes that name their administrative city (JKTS = Jakarta Selatan).
_JAKARTA_BY_CODE = {
    "JKTS": "Jakarta Selatan", "JKTU": "Jakarta Utara", "JKTB": "Jakarta Barat", "JKTT": "Jakarta Timur",
    "JKTP": "Jakarta Pusat",
}

# Display name of a city where plain title case reads wrong.
_CITY_DISPLAY = {"bsd": "BSD", "solo": "Solo", "pare pare": "Parepare", "dili": "Dili"}


def _norm(text: str | None) -> str:
    """'Kab. Banyumas' / 'KABUPATEN BANYUMAS' -> 'banyumas'; 'Kota Bandung' -> 'bandung'."""
    text = re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()
    return re.sub(r"^(kabupaten|kab|kota|city of)\s+", "", text)


# A curated city may be written as the city ("Purwokerto") or as its regency ("Kab. Banyumas").
_CITY_BY_NAME: dict[str, str] = {name: name for name in REGENCY_OF_CITY}
for _city, (_regency, _) in REGENCY_OF_CITY.items():
    _CITY_BY_NAME.setdefault(_norm(_regency), _city)


@dataclass(frozen=True)
class Place:
    province: str
    regency: str
    city: str | None  # what the POP is called on the map ("Purwokerto"), None when unknown


def _display(city: str) -> str:
    return _CITY_DISPLAY.get(city, city.title())


def _city_of_code(code: str) -> str | None:
    entry = CITIES.get(code) or CITIES.get(code[:3])
    return entry[0] if entry else None


def _fits(province: str, area: str | None) -> bool:
    """A province may hold a POP of `area`; an unmapped or custom (non-standard) area accepts any."""
    if area not in AREAS:
        return True
    return area in _AREAS_OF_PROVINCE.get(province, {area})


def _city_of_name(name: str | None) -> str | None:
    """City in a POP name: the whole name or its last ' - ' part ('Puwokerto - Puwokerto Timur'), else a city word."""
    for part in (name, (name or "").split(" - ")[-1]):
        if (found := _CITY_BY_NAME.get(_norm(part))) is not None:
            return found
    named_code = city_code_for_hostname(name)
    return _city_of_code(named_code) if named_code else None


def _cell_site_town(hostnames: Iterable[str]) -> tuple[str, str] | None:
    """(province code, town code) most cell-site hostnames of the POP carry, if any."""
    found = Counter((parts[1], parts[2]) for h in hostnames if (parts := csr_parts(h)))
    return found.most_common(1)[0][0] if found else None


def _province_hint(code: str, name: str | None, town: tuple[str, str] | None) -> str | None:
    """Province without a city: a province word as code/name, else the cell-site hostnames' province."""
    for text in (code, name):
        if (province := _PROVINCE_ALIASES.get(_norm(text))) is not None:
            return province
    return _PROVINCE_BY_CSR_CODE.get(town[0]) if town else None


def place_for_pop(code: str, city: str | None, name: str | None, area: str | None = None,
                  hostnames: Iterable[str] = ()) -> Place:
    """Province, regency and city label of a POP in `area`. Unknown parts get the 'belum dipetakan' names."""
    code = (code or "").strip().upper()
    if code == "UNASSIGNED":
        return Place(UNMAPPED_PROVINCE, UNMAPPED_REGENCY, None)
    if code in _JAKARTA_BY_CODE and _fits(_JK, area):
        return Place(_JK, _JAKARTA_BY_CODE[code], _JAKARTA_BY_CODE[code])
    curated = (city or "").strip()
    candidates = [(_CITY_BY_NAME.get(_norm(curated)), True), (_city_of_name(name), False), (_city_of_code(code), False)]
    for candidate, is_curated in candidates:
        if candidate in REGENCY_OF_CITY and _fits(REGENCY_OF_CITY[candidate][1], area):
            regency, province = REGENCY_OF_CITY[candidate]
            return Place(province, regency, curated if is_curated else _display(candidate))
    # A curated city we have no regency for still names the POP; one that contradicts the area does not.
    label = curated if curated and candidates[0][0] is None and curated.lower() != "unknown" else None
    town = _cell_site_town(hostnames)
    province = _province_hint(code, name, town)
    if province and _fits(province, area) and town in _CSR_TOWNS and _PROVINCE_BY_CSR_CODE.get(town[0]) == province:
        town_name, regency = _CSR_TOWNS[town]
        return Place(province, regency, town_name)
    if province and _fits(province, area):
        return Place(province, UNMAPPED_REGENCY, label)
    return Place(UNMAPPED_PROVINCE, UNMAPPED_REGENCY, label)
