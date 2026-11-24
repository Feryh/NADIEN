"""SNI 7657:2010 (Singkatan nama kota): the official 3-letter code of every kabupaten/kota, with its capital.

`reference/sni_7657_2010.csv` holds the standard's table (481 codes; a kota and the kabupaten around it may share one,
in which case the kota is kept). Regencies formed after 2010 are not in it. The network does not follow the
standard everywhere (MGL is Magelang here, Menggala in the standard), so callers use it only after the codes the
team curated in `areas.CITIES`, and only when it agrees with what is known of the POP.
"""

import csv
import re
from dataclasses import dataclass
from functools import cache
from pathlib import Path

# Not under data/: .gitignore keeps data/ for exports of real network data.
TABLE = Path(__file__).resolve().parent.parent / "reference" / "sni_7657_2010.csv"
_CODE = re.compile(r"^([A-Z]{3})\d*$")  # BLN, BLN2; not JKTB or ATN910


@dataclass(frozen=True)
class SniPlace:
    code: str
    regency: str  # "Kab. Tanah Bumbu", "Kota Kupang"
    capital: str  # ibu kota: "Batulicin"
    province: str


def _regency_label(name: str) -> str:
    return re.sub(r"^Kabupaten\s+", "Kab. ", name.strip())


@cache
def table() -> dict[str, SniPlace]:
    with TABLE.open(encoding="utf-8", newline="") as source:
        return {row["code"]: SniPlace(row["code"], _regency_label(row["regency"]), row["capital"], row["province"])
                for row in csv.DictReader(source)}


def sni_code(pop_code: str | None) -> str | None:
    """The 3-letter code a POP code is written in, if any (BLN2 -> BLN)."""
    match = _CODE.match((pop_code or "").strip().upper())
    return match.group(1) if match else None


def place_for_code(pop_code: str | None) -> SniPlace | None:
    code = sni_code(pop_code)
    return table().get(code) if code else None
