"""Export / import the area mapping work list as CSV (opens directly in Excel).

    python -m app.area_mapping export [folder]   # default: ../data
    python -m app.area_mapping import [folder]   # backs up the DB first

Files:
  pop-area-belum-dipetakan.csv   kode;nama;perangkat;contoh_hostname;petunjuk;area   <- fill `area`
  perangkat-unassigned.csv       hostname;vendor;role;petunjuk;kode_pop              <- fill `kode_pop`

Semicolon-separated with a UTF-8 BOM, the format Excel uses for Indonesian locales.
Rows left empty are skipped; nothing already set is overwritten except by an explicit value.
"""

import csv
import sys
from collections import Counter, defaultdict
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import models as m
from .backup import create_backup, is_backupable
from .database import SessionLocal, engine
from .services.areas import (
    AREA_ORDER,
    UNMAPPED_AREA,
    area_for_hostname,
    area_for_hostnames,
    area_for_pop,
    city_label,
    pop_code_for_hostname,
)

POPS_FILE = "pop-area-belum-dipetakan.csv"
DEVICES_FILE = "perangkat-unassigned.csv"
UNASSIGNED = "UNASSIGNED"
VALID_AREAS = [a for a in AREA_ORDER if a != UNMAPPED_AREA]


def _hint(hostnames: list[str]) -> str:
    """Locations named in the hostnames, e.g. 'Sumatera 3, Jabodetabek 2' — a lead, not an answer."""
    found = Counter(a for h in hostnames if (a := area_for_hostname(h)))
    return ", ".join(f"{area} {n}" for area, n in found.most_common())


def export(db: Session, folder: Path) -> tuple[Path, Path, int, int]:
    folder.mkdir(parents=True, exist_ok=True)
    pops = {p.id: p for p in db.scalars(select(m.Pop))}
    hosts: dict[int, list[m.Device]] = defaultdict(list)
    for d in db.scalars(select(m.Device).order_by(m.Device.hostname)):
        hosts[d.pop_id].append(d)
    vendors = {v.id: v.name for v in db.scalars(select(m.Vendor))}

    pop_rows = []
    for pid, pop in pops.items():
        if pop.code.upper() == UNASSIGNED:
            continue
        names = [d.hostname for d in hosts[pid]]
        if area_for_pop(pop.code, pop.region, pop.name) != UNMAPPED_AREA or area_for_hostnames(names):
            continue
        pop_rows.append([pop.code, pop.name, len(names), " | ".join(names[:4]), _hint(names), ""])
    pop_rows.sort(key=lambda r: (-r[2], r[0]))

    unassigned = next((p for p in pops.values() if p.code.upper() == UNASSIGNED), None)
    dev_rows = []
    for d in hosts[unassigned.id] if unassigned else []:
        code = pop_code_for_hostname(d.hostname)
        hint = f"kota {city_label(code)} ({code})" if code else (area_for_hostname(d.hostname) or "")
        dev_rows.append([d.hostname, vendors.get(d.vendor_id or -1, ""), d.role, hint, ""])

    pops_path, devices_path = folder / POPS_FILE, folder / DEVICES_FILE
    with pops_path.open("w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh, delimiter=";")
        w.writerow(["kode", "nama", "perangkat", "contoh_hostname", "petunjuk", "area"])
        w.writerows(pop_rows)
        w.writerow([])
        w.writerow(["# Isi kolom 'area' dengan salah satu:", " | ".join(VALID_AREAS)])
        w.writerow(["# Area baru juga boleh (mis. 'Priangan Timur'); kosongkan baris yang belum diketahui."])
    with devices_path.open("w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh, delimiter=";")
        w.writerow(["hostname", "vendor", "role", "petunjuk", "kode_pop"])
        w.writerows(dev_rows)
        w.writerow([])
        w.writerow(["# Isi 'kode_pop' dengan kode POP yang sudah ada (mis. YGY) atau kode baru; kosongkan jika belum tahu."])
    return pops_path, devices_path, len(pop_rows), len(dev_rows)


def _rows(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8-sig", newline="") as fh:
        sample = fh.read(2048)
        fh.seek(0)
        delimiter = ";" if sample.count(";") >= sample.count(",") else ","
        return [
            {(k or "").strip().lower(): (v or "").strip() for k, v in row.items()}
            for row in csv.DictReader(fh, delimiter=delimiter)
            if row and not str(next(iter(row.values()), "") or "").startswith("#")
        ]


def import_(db: Session, folder: Path) -> dict:
    pops = {p.code.upper(): p for p in db.scalars(select(m.Pop))}
    report: dict = {"pops_updated": [], "devices_moved": [], "pops_created": [], "warnings": []}

    for row in _rows(folder / POPS_FILE):
        code, area = row.get("kode", "").upper(), row.get("area", "")
        if not code or not area:
            continue
        pop = pops.get(code)
        if pop is None:
            report["warnings"].append(f"POP {code} tidak ditemukan — dilewati")
            continue
        if area not in VALID_AREAS:
            report["warnings"].append(f"{code}: area baru '{area}' (bukan salah satu area standar)")
        pop.region = area
        report["pops_updated"].append(f"{code} -> {area}")

    devices = {d.hostname.upper(): d for d in db.scalars(select(m.Device))}
    for row in _rows(folder / DEVICES_FILE):
        hostname, code = row.get("hostname", ""), row.get("kode_pop", "").upper()
        if not hostname or not code:
            continue
        device = devices.get(hostname.upper())
        if device is None:
            report["warnings"].append(f"Perangkat {hostname} tidak ditemukan — dilewati")
            continue
        pop = pops.get(code)
        if pop is None:
            area = area_for_pop(code, None, None)
            pop = m.Pop(code=code, name=city_label(code) or code, status="active",
                        region=None if area == UNMAPPED_AREA else area)
            db.add(pop)
            db.flush()
            pops[code] = pop
            report["pops_created"].append(code)
        device.pop_id = pop.id
        device.pop_locked = True  # the mapping file is a manual choice
        report["devices_moved"].append(f"{device.hostname} -> {code}")
    db.commit()
    return report


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if not args or args[0] not in ("export", "import"):
        print(__doc__)
        return 2
    folder = Path(args[1]) if len(args) > 1 else Path(__file__).resolve().parents[2] / "data"
    with SessionLocal() as db:
        if args[0] == "export":
            pops_path, devices_path, n_pops, n_devs = export(db, folder)
            print(f"{n_pops} POP -> {pops_path}\n{n_devs} perangkat -> {devices_path}")
            return 0
        if is_backupable(engine):
            print("backup:", create_backup(engine, "area-import").name)
        report = import_(db, folder)
    print(f"POP diperbarui: {len(report['pops_updated'])}, perangkat dipindah: {len(report['devices_moved'])}, "
          f"POP baru: {', '.join(report['pops_created']) or '-'}")
    for line in report["warnings"]:
        print("peringatan:", line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
