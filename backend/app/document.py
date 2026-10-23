"""The tutela document: one layout rendered as plain text, .docx (python-docx) and PDF (fpdf2).

The layout follows the usual Colombian format: addressed to the judge on duty (reparto), a reference
block, numbered sections, notifications and signature. Personal data Amparo never asks for is left as
[COMPLETAR: ...] placeholders, highlighted in the .docx and PDF so the person sees what to fill in.
"""

import io
import re
import unicodedata
from dataclasses import dataclass
from typing import Literal

from app.schemas import TutelaSections

Kind = Literal["title", "line", "heading", "para", "item", "closing", "signature"]

PLACEHOLDER_RE = re.compile(r"\[COMPLETAR[^\]]*\]")
NOMBRE = "[COMPLETAR: nombre completo]"
CEDULA = "[COMPLETAR: número de cédula]"


@dataclass
class Block:
    kind: Kind
    text: str
    number: int | None = None


def _clean_item(item: str) -> str:
    # Models often number items themselves; drop that so numbering is not doubled.
    return re.sub(r"^\s*\d+\s*[.)-]\s*", "", item).strip()


def _right_name(right: str) -> str:
    """'Derecho a la Salud' -> 'la salud', so it reads well after 'mis derechos fundamentales a'."""
    name = re.sub(r"\s*\([^)]*\)", "", right).strip()  # "(artículo 49 de la Constitución)" stays in section II
    name = re.sub(r"^(el\s+)?derecho\s+(fundamental\s+)?(a\s+|al\s+)?", "", name, flags=re.IGNORECASE)
    return name[:1].lower() + name[1:]


# Placeholders such as [NOMBRE COMPLETO] or [CIUDAD] that a model may still write.
_LEGACY_PLACEHOLDER_RE = re.compile(r"\[(?!COMPLETAR)([A-ZÁÉÍÓÚÑ][A-ZÁÉÍÓÚÑ .]{2,})\]")


def normalize_placeholders(text: str) -> str:
    return _LEGACY_PLACEHOLDER_RE.sub(lambda m: f"[COMPLETAR: {m[1].strip().lower()}]", text)


def _items(values: list[str]) -> list[Block]:
    items = [_clean_item(v) for v in values]
    return [Block("item", text, i) for i, text in enumerate((t for t in items if t), 1)]


def _paragraphs(text: str) -> list[Block]:
    return [Block("para", p.strip()) for p in re.split(r"\n\s*\n|\n", text) if p.strip()]


def tutela_blocks(accionada: str, ciudad: str | None, s: TutelaSections) -> list[Block]:
    s = TutelaSections(**{
        k: [normalize_placeholders(x) for x in v] if isinstance(v, list) else normalize_placeholders(v)
        for k, v in s.model_dump().items()
    })
    lugar = ciudad or "[COMPLETAR: ciudad o municipio]"
    derechos = ", ".join(_right_name(r) for r in s.derechos_vulnerados)
    intro = (
        f"{NOMBRE}, mayor de edad, identificado(a) con la cédula de ciudadanía No. {CEDULA}, domiciliado(a) en "
        f"{lugar}, actuando en nombre propio, respetuosamente presento ACCIÓN DE TUTELA contra {accionada}, con "
        "fundamento en el artículo 86 de la Constitución Política y el Decreto 2591 de 1991, para que se protejan "
        f"mis derechos fundamentales a {derechos}, con base en los siguientes:"
    )
    return [
        Block("title", "Señor(a)"),
        Block("title", "JUEZ(A) DE LA REPÚBLICA (REPARTO)"),
        Block("line", lugar),
        Block("line", "E. S. D."),
        Block("line", "REFERENCIA: ACCIÓN DE TUTELA"),
        Block("line", f"ACCIONANTE: {NOMBRE}, C.C. {CEDULA}"),
        Block("line", f"ACCIONADA: {accionada}"),
        Block("line", f"DERECHOS INVOCADOS: {derechos}"),
        Block("para", intro),
        Block("heading", "I. HECHOS"),
        *_items(s.hechos),
        Block("heading", "II. DERECHOS FUNDAMENTALES VULNERADOS"),
        *_items(s.derechos_vulnerados),
        Block("heading", "III. FUNDAMENTOS JURÍDICOS"),
        *_paragraphs(s.fundamentos_juridicos),
        Block("heading", "IV. PRETENSIONES"),
        Block("para", "Con fundamento en lo anterior, solicito respetuosamente al señor(a) juez(a):"),
        *_items(s.pretensiones),
        Block("heading", "V. PRUEBAS Y ANEXOS"),
        *_items(s.pruebas_y_anexos),
        Block("heading", "VI. JURAMENTO"),
        *_paragraphs(s.juramento),
        Block("heading", "VII. NOTIFICACIONES"),
        Block("para", "Accionante: dirección [COMPLETAR: dirección], teléfono [COMPLETAR: teléfono], "
                      "correo electrónico [COMPLETAR: correo electrónico]."),
        Block("para", f"Accionada: {accionada}, en su dirección o correo de notificaciones judiciales "
                      "[COMPLETAR: dirección o correo de notificaciones judiciales de la EPS]."),
        Block("closing", "Atentamente,"),
        Block("signature", "______________________________"),
        Block("signature", NOMBRE),
        Block("signature", f"C.C. {CEDULA} de [COMPLETAR: lugar de expedición]"),
        Block("signature", "Teléfono: [COMPLETAR: teléfono]"),
        Block("signature", "Correo electrónico: [COMPLETAR: correo electrónico]"),
    ]


def render_text(blocks: list[Block]) -> str:
    out: list[str] = []
    previous: Kind | None = None
    for b in blocks:
        text = f"{b.number}. {b.text}" if b.number is not None else b.text
        if b.kind == "heading" or (b.kind in ("para", "closing") and previous not in (None, "para", "item")):
            out.append("")
        if b.kind == "line" and b.text.startswith("REFERENCIA"):
            out.append("")
        if b.kind == "signature" and previous == "closing":
            out += ["", ""]
        out.append(text)
        if b.kind == "heading":
            out.append("")
        previous = b.kind
    return "\n".join(out).strip() + "\n"


# --- .docx ---------------------------------------------------------------------


def _docx_runs(paragraph, text: str, bold: bool = False) -> None:  # paragraph: a python-docx Paragraph
    from docx.enum.text import WD_COLOR_INDEX

    pos = 0
    for m in PLACEHOLDER_RE.finditer(text):
        if m.start() > pos:
            paragraph.add_run(text[pos:m.start()]).bold = bold
        run = paragraph.add_run(m.group())
        run.bold = True
        run.font.highlight_color = WD_COLOR_INDEX.YELLOW
        pos = m.end()
    if pos < len(text):
        paragraph.add_run(text[pos:]).bold = bold


def render_docx(blocks: list[Block]) -> bytes:
    from docx import Document
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.shared import Cm, Pt

    doc = Document()
    section = doc.sections[0]
    section.page_height, section.page_width = Cm(27.94), Cm(21.59)  # carta
    section.top_margin = section.bottom_margin = Cm(2.5)
    section.left_margin = section.right_margin = Cm(3)
    normal = doc.styles["Normal"]
    normal.font.name = "Arial"
    normal.font.size = Pt(11.5)
    normal.paragraph_format.space_after = Pt(6)
    normal.paragraph_format.line_spacing = 1.15
    doc.core_properties.title = "Acción de tutela"
    doc.core_properties.author = "Borrador generado con Amparo"

    previous: Kind | None = None
    for b in blocks:
        p = doc.add_paragraph()
        fmt = p.paragraph_format
        if b.kind == "title":
            _docx_runs(p, b.text, bold=True)
            fmt.space_after = Pt(0)
        elif b.kind == "line":
            _docx_runs(p, b.text, bold=b.text.startswith(("REFERENCIA", "E. S. D.")))
            fmt.space_after = Pt(0)
            if b.text.startswith("REFERENCIA"):
                fmt.space_before = Pt(12)
        elif b.kind == "heading":
            _docx_runs(p, b.text, bold=True)
            fmt.space_before = Pt(12)
            fmt.keep_with_next = True
        elif b.kind == "item":
            _docx_runs(p, f"{b.number}. {b.text}")
            fmt.left_indent = Cm(0.6)
            fmt.first_line_indent = Cm(-0.6)
            p.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY
        elif b.kind == "para":
            _docx_runs(p, b.text)
            p.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY
            if previous == "line":
                fmt.space_before = Pt(12)
        elif b.kind == "closing":
            _docx_runs(p, b.text)
            fmt.space_before = Pt(12)
            fmt.space_after = Pt(36)
        elif b.kind == "signature":
            _docx_runs(p, b.text, bold=b.text == NOMBRE)
            fmt.space_after = Pt(0)
        previous = b.kind

    buffer = io.BytesIO()
    doc.save(buffer)
    return buffer.getvalue()


# --- PDF -------------------------------------------------------------------------

# fpdf2's built-in fonts cover Latin-1, which has every Spanish letter; map the few typographic
# characters models like to use, so no system or bundled font is needed on Cloud Run.
_PDF_REPLACEMENTS = str.maketrans({
    "“": '"', "”": '"', "‘": "'", "’": "'", "–": "-", "—": "-", "…": "...", "•": "-", " ": " ",
    "≥": ">=", "≤": "<=",
})


def _latin1(text: str) -> str:
    text = text.translate(_PDF_REPLACEMENTS)
    return text.encode("latin-1", "replace").decode("latin-1")


def _pdf_markdown(text: str, bold: bool = False) -> str:
    """fpdf2 markdown: **bold**; placeholders are bold. Escape stray asterisks first."""
    text = _latin1(text).replace("*", "\\*")
    text = PLACEHOLDER_RE.sub(lambda m: f"**{m.group()}**" if not bold else m.group(), text)
    return f"**{text}**" if bold else text


def render_pdf(blocks: list[Block]) -> bytes:
    from fpdf import FPDF

    pdf = FPDF(format="letter", unit="mm")
    pdf.set_margins(30, 25, 30)
    pdf.set_auto_page_break(True, margin=25)
    pdf.set_title("Acción de tutela")
    pdf.set_author("Borrador generado con Amparo")
    pdf.add_page()
    pdf.set_font("Helvetica", size=11)
    line_h = 5.6

    def write(text: str, bold: bool = False, align: str = "J", indent: float = 0.0, after: float = 2.0) -> None:
        pdf.set_x(pdf.l_margin + indent)
        pdf.multi_cell(pdf.epw - indent, line_h, _pdf_markdown(text, bold), align=align, markdown=True,
                       new_x="LMARGIN", new_y="NEXT")
        pdf.ln(after)

    previous: Kind | None = None
    for b in blocks:
        if b.kind == "title":
            write(b.text, bold=True, align="L", after=0)
        elif b.kind == "line":
            if b.text.startswith("REFERENCIA"):
                pdf.ln(5)
            write(b.text, bold=b.text.startswith(("REFERENCIA", "E. S. D.")), align="L", after=0)
        elif b.kind == "heading":
            pdf.ln(4)
            if pdf.get_y() > pdf.h - pdf.b_margin - 25:  # keep a heading with its first lines
                pdf.add_page()
            write(b.text, bold=True, align="L")
        elif b.kind == "item":
            pdf.set_x(pdf.l_margin)
            pdf.cell(6, line_h, f"{b.number}.")
            write(b.text, indent=6)
        elif b.kind == "para":
            if previous == "line":
                pdf.ln(5)
            write(b.text)
        elif b.kind == "closing":
            pdf.ln(4)
            write(b.text, align="L", after=14)
        elif b.kind == "signature":
            write(b.text, bold=b.text == NOMBRE, align="L", after=0)
        previous = b.kind
    return bytes(pdf.output())


def ascii_fold(text: str) -> str:
    return unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")


CONTENT_TYPES = {
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "pdf": "application/pdf",
}


def filename(accionada: str, fmt: str) -> str:
    """'EPS Salud Andina' -> 'tutela-eps-salud-andina.docx'."""
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_fold(accionada).lower()).strip("-")[:60].strip("-") or "eps"
    return f"tutela-{slug}.{fmt}"
