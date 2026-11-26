"""IPv6 plan as an Excel workbook for the backbone team: one sheet per view plus findings and assumptions."""

import io
from datetime import datetime, timezone

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from .ipv6_plan import Plan, summary

FONT = "Arial"
HEAD_FILL = PatternFill("solid", fgColor="1F3864")
HEAD_FONT = Font(name=FONT, bold=True, color="FFFFFF")
BODY_FONT = Font(name=FONT, size=10)
WARN_FILL = PatternFill("solid", fgColor="FCE4D6")

ASSUMPTIONS = [
    ("Sumber", "Register kota: sheet City Register yang diimport ke NADIEN. Perangkat: inventory NADIEN."),
    ("Node locator", "2400:dc00:RCCN::/48 = blok kota + node index; loopback locator::1/128; End SID locator:e000::."),
    ("Band", "25% pertama blok kota = backbone (standard 0x00-0x0F, metro 0x00-0x3F), sisanya akses."),
    ("Kelompok backbone (PROVISIONAL, sheet 03A)", "Standard: P/CORE 0x00-0x03, BNG 0x04-0x07, PE/PEIN/lainnya "
     "0x08-0x0F. Metro: P/CORE 0x00-0x0F, BNG 0x10-0x1F, PE/PEIN/lainnya 0x20-0x3F."),
    ("OAM L2 (PROVISIONAL, sheet 03A)", "OAM /48 kota: :1::/64 OLT, :2::/64 switch, :3::/64 DCI, :4::/64 router "
     "non-SRv6 / manajemen; host urut."),
    ("Klasifikasi", "Backbone: p, bng, pe, agg dengan PEIN. Akses: agg lain, csr. Hanya Huawei, Cisco, ZTE, FiberHome, "
     "Juniper, Nokia sebagai node SRv6; vendor lain di router dan olt/switch/access/dci diberi alamat OAM. CPE tidak "
     "dihitung."),
    ("IS-IS", "Backbone L2 49.0001; akses L1 49.0RCC per kota."),
    ("Status", "baru = usulan belum disimpan; proposed = tersimpan di NADIEN; confirmed = disetujui / terkonfigurasi."),
]


def _sheet(wb: Workbook, title: str, headers: list[str], rows: list[list], widths: list[int], warn_col: int | None = None,
           warn_values: tuple = ()) -> None:
    ws = wb.create_sheet(title)
    ws.append(headers)
    for cell in ws[1]:
        cell.fill, cell.font = HEAD_FILL, HEAD_FONT
        cell.alignment = Alignment(wrap_text=True, vertical="center")
    for row in rows:
        ws.append(row)
    for row in ws.iter_rows(min_row=2):
        for cell in row:
            cell.font = BODY_FONT
        if warn_col is not None and row[warn_col].value in warn_values:
            for cell in row:
                cell.fill = WARN_FILL
    for i, width in enumerate(widths, 1):
        ws.column_dimensions[get_column_letter(i)].width = width
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = f"A1:{get_column_letter(len(headers))}{max(len(rows), 1) + 1}"


def export_xlsx(plan: Plan) -> bytes:
    wb = Workbook()
    first = wb.active
    first.title = "Ringkasan"
    s = summary(plan)
    first["A1"] = "Rencana IPv6 / SRv6 — NADIEN"
    first["A1"].font = Font(name=FONT, size=14, bold=True)
    first["A2"] = f"Dibuat {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC dari register dan inventory NADIEN."
    lines = [("Kota di register", s["cities"]), ("Router dengan node locator", s["routers"]),
             ("  band backbone", s["backbone"]), ("  band akses", s["access"]),
             ("Router tanpa locator (band penuh)", s["overflow"]), ("Perangkat L2 dengan alamat OAM", s["oam"]),
             ("Perangkat belum terpetakan", s["unmapped_devices"]), ("POP belum ada di register", s["unmapped_pops"]),
             ("POP dengan kode berbeda dari register", s["code_conflicts"]),
             ("Kota perlu blok kedua (50%)", ", ".join(s["promote"]) or "-"),
             ("Alokasi belum disimpan", s["unsaved"])]
    for i, (label, value) in enumerate(lines, 4):
        first.cell(row=i, column=1, value=label).font = BODY_FONT
        first.cell(row=i, column=2, value=value).font = Font(name=FONT, size=10, bold=True)
    first.column_dimensions["A"].width, first.column_dimensions["B"].width = 42, 30

    _sheet(wb, "Node Locator",
           ["Hostname", "Role", "Vendor", "POP", "Kota", "Region", "Band", "Kelompok", "Node index", "Node locator /48",
            "Loopback /128", "End SID", "IS-IS area", "Loopback IPv4", "Dasar mapping", "Status", "Catatan"],
           [[r["hostname"], r["role"], r["vendor"], r["pop_code"], r["city"], r["region"], r["band"], r["group"],
             f"0x{r['node_index']:02X}" if r["node_index"] is not None else "", r["locator"], r["loopback6"],
             r["end_sid"], r["isis_area"], r["loopback4"], r["how"], r["status"], r["note"]] for r in plan.routers],
           [30, 8, 11, 8, 10, 15, 10, 15, 8, 24, 28, 26, 10, 15, 20, 10, 32], warn_col=6, warn_values=("overflow",))
    _sheet(wb, "OAM L2",
           ["Hostname", "Role", "Vendor", "POP", "Kota", "Region", "OAM /48", "Kelompok", "Subnet /64", "Alamat /128",
            "Dasar mapping", "Status"],
           [[r["hostname"], r["role"], r["vendor"], r["pop_code"], r["city"], r["region"], r["oam48"], r["group"],
             r["subnet"], r["address"], r["how"], r["status"]] for r in plan.oam],
           [32, 8, 11, 8, 10, 15, 22, 24, 24, 28, 20, 10])
    _sheet(wb, "Kapasitas Kota",
           ["Region", "Kota", "Nama", "Tier", "Index", "Blok infra", "Backbone", "Backbone locators", "Backbone fit",
            "Akses", "Access locators", "Fit vs 50%", "Tanpa locator", "Perangkat OAM", "OAM /48", "Residential"],
           [[c["region"], c["code"], c["name"], c["tier"], c["city_index"], c["infra_block"], c["backbone"],
             c["backbone_size"], c["backbone_fit"], c["access"], c["access_size"], c["fit_50"], c["overflow"], c["oam"],
             c["oam48"], c["residential_block"]] for c in plan.capacity],
           [15, 10, 20, 10, 7, 22, 9, 9, 9, 8, 9, 28, 9, 9, 22, 22], warn_col=11,
           warn_values=("PROMOTE - needs a second city block",))
    _sheet(wb, "Mapping POP",
           ["POP", "Nama POP", "Area", "Perangkat", "Kota", "Metode", "Keputusan", "Region", "Usulan kota induk",
            "Jarak (km)", "Catatan"],
           [[p.pop_code, p.pop_name, p.area, p.devices, p.city, p.method, p.decision, p.region, p.suggested_city,
             p.suggested_km, p.note] for p in plan.pops if p.devices],
           [10, 30, 20, 9, 10, 18, 10, 15, 12, 9, 60], warn_col=5, warn_values=("unmapped",))
    _sheet(wb, "Belum Terpetakan", ["Hostname", "Role", "Jenis", "POP", "Nama POP", "Region", "Usulan kota induk"],
           [[u["hostname"], u["role"], u["kind"], u["pop_code"], u["pop_name"], u["region"], u["suggested_city"]]
            for u in plan.unmapped], [34, 9, 9, 9, 30, 15, 12])
    _sheet(wb, "Asumsi", ["Topik", "Aturan"], [list(a) for a in ASSUMPTIONS], [40, 120])
    buffer = io.BytesIO()
    wb.save(buffer)
    return buffer.getvalue()
