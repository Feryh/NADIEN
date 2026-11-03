"""Render the MoP content from services/mop.py to PDF (fpdf2, core fonts)."""

from typing import Any

from fpdf import FPDF
from fpdf.enums import XPos, YPos
from fpdf.fonts import FontFace

# Core PDF fonts are latin-1 only; map the few symbols the MoP text uses.
_REPLACEMENTS = {"—": "-", "–": "-", "↔": "<->", "→": "->", "·": "|", "≤": "<=", "≥": ">=", "…": "...",
                 "“": '"', "”": '"', "‘": "'", "’": "'"}
NEXT_LINE = {"new_x": XPos.LMARGIN, "new_y": YPos.NEXT}
HEADING = FontFace(emphasis="BOLD", fill_color=(225, 232, 242))


def latin1(text: Any) -> str:
    out = str(text if text is not None else "")
    for src, dst in _REPLACEMENTS.items():
        out = out.replace(src, dst)
    return out.encode("latin-1", "replace").decode("latin-1")


class _MopPdf(FPDF):
    def __init__(self, title: str, draft: bool):
        super().__init__(format="A4")
        self.doc_title, self.draft = latin1(title), draft
        self.set_auto_page_break(auto=True, margin=18)
        self.set_margins(16, 16, 16)

    def header(self) -> None:
        if self.page_no() == 1:
            return
        self.set_font("Helvetica", "I", 8)
        self.set_text_color(110)
        self.cell(0, 6, self.doc_title + ("  [DRAFT]" if self.draft else ""), **NEXT_LINE)
        self.set_text_color(0)
        self.ln(2)

    def footer(self) -> None:
        self.set_y(-12)
        self.set_font("Helvetica", "I", 8)
        self.set_text_color(110)
        self.cell(0, 6, f"NADIEN - halaman {self.page_no()}/{{nb}}", align="C")
        self.set_text_color(0)


def render_mop_pdf(doc: dict[str, Any]) -> bytes:
    pdf = _MopPdf(doc["title"], doc["draft"])
    pdf.alias_nb_pages()
    pdf.add_page()
    _cover(pdf, doc)
    for section in doc["sections"]:
        _section(pdf, section)
    return bytes(pdf.output())


def _cover(pdf: _MopPdf, doc: dict[str, Any]) -> None:
    pdf.set_font("Helvetica", "B", 18)
    pdf.multi_cell(0, 9, latin1(doc["title"]), **NEXT_LINE)
    pdf.set_font("Helvetica", "", 11)
    pdf.multi_cell(0, 6, latin1(doc["subtitle"]), **NEXT_LINE)
    pdf.ln(3)
    if doc["draft"]:
        pdf.set_fill_color(253, 226, 226)
        pdf.set_text_color(160, 20, 20)
        pdf.set_font("Helvetica", "B", 11)
        pdf.multi_cell(0, 8, "DRAFT - validasi desain masih GAGAL. Jangan dieksekusi sebelum temuan diselesaikan.",
                       border=1, fill=True, **NEXT_LINE)
        pdf.set_text_color(0)
        pdf.ln(3)
    meta = doc["meta"]
    window = " s/d ".join(v for v in (meta.get("window_start"), meta.get("window_end")) if v)
    rows = [["Nomor MoP", meta.get("number") or "-"], ["Nomor change", meta.get("change_ticket") or "-"],
            ["Window", window or "-"], ["Dibuat", doc["generated_at"].replace("T", " ").replace("Z", " UTC")],
            ["Status validasi", doc["validation_status"].upper()]]
    _table(pdf, {"headers": ["Dokumen", ""], "rows": rows}, widths=[45, 0])
    pdf.ln(4)
    pdf.set_font("Helvetica", "B", 10)
    pdf.cell(0, 6, "Persetujuan", **NEXT_LINE)
    _signatures(pdf, [("Pelaksana", meta.get("executor")), ("Reviewer", meta.get("reviewer")),
                      ("Approver", meta.get("approver"))])
    pdf.ln(4)


def _signatures(pdf: _MopPdf, people: list[tuple[str, str | None]]) -> None:
    width = (pdf.w - pdf.l_margin - pdf.r_margin) / len(people)
    pdf.set_font("Helvetica", "", 9)
    top = pdf.get_y()
    for i, (role, name) in enumerate(people):
        x = pdf.l_margin + i * width
        pdf.rect(x, top, width, 28)
        pdf.set_xy(x + 2, top + 2)
        pdf.cell(width - 4, 5, latin1(role))
        pdf.set_xy(x + 2, top + 21)
        pdf.cell(width - 4, 5, latin1(name or "Nama / tanggal"))
    pdf.set_xy(pdf.l_margin, top + 30)


def _section(pdf: _MopPdf, section: dict[str, Any]) -> None:
    if pdf.get_y() > pdf.h - 50:
        pdf.add_page()
    pdf.ln(2)
    pdf.set_font("Helvetica", "B", 13)
    pdf.set_text_color(20, 60, 120)
    pdf.cell(0, 8, latin1(section["title"]), **NEXT_LINE)
    pdf.set_text_color(0)
    pdf.set_font("Helvetica", "", 10)
    for paragraph in section["paragraphs"]:
        pdf.multi_cell(0, 5, latin1(paragraph), **NEXT_LINE)
        pdf.ln(1)
    for table in section["tables"]:
        if table.get("caption"):
            pdf.set_font("Helvetica", "B", 10)
            pdf.cell(0, 6, latin1(table["caption"]), **NEXT_LINE)
        _table(pdf, table)
        pdf.ln(2)
    for step in section["steps"]:
        _step(pdf, step)


def _table(pdf: _MopPdf, table: dict[str, Any], widths: list[float] | None = None) -> None:
    usable = pdf.w - pdf.l_margin - pdf.r_margin
    columns = len(table["headers"])
    if widths is None and columns == 2:  # key/value tables: narrow key column
        widths = [45, 0]
    if widths:
        rest = usable - sum(w for w in widths if w)
        col_widths = [w or rest / widths.count(0) for w in widths]
    else:
        col_widths = None
    pdf.set_font("Helvetica", "", 9)
    with pdf.table(col_widths=tuple(col_widths) if col_widths else None, text_align="LEFT", line_height=5,
                   first_row_as_headings=any(table["headers"]), headings_style=HEADING, width=usable) as grid:
        if any(table["headers"]):
            grid.row([latin1(h) for h in table["headers"]])
        for row in table["rows"]:
            cells = list(row) + [""] * (columns - len(row))
            grid.row([latin1(c) for c in cells])


def _step(pdf: _MopPdf, step: dict[str, Any]) -> None:
    if pdf.get_y() > pdf.h - 40:
        pdf.add_page()
    pdf.set_font("Helvetica", "B", 10)
    pdf.multi_cell(0, 6, latin1(f"{step['title']}  [{step['dialect_label']}]"), **NEXT_LINE)
    if step["commands"]:
        pdf.set_font("Courier", "", 8.5)
        pdf.set_fill_color(242, 244, 247)
        pdf.multi_cell(0, 4.2, latin1("\n".join(step["commands"])), border=0, fill=True, **NEXT_LINE)
    if step["note"]:
        pdf.set_font("Helvetica", "I", 9)
        pdf.multi_cell(0, 5, latin1(step["note"]), **NEXT_LINE)
    pdf.set_font("Helvetica", "", 10)
    pdf.ln(2)
