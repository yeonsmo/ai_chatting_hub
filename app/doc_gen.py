"""문서 생성기 — AI가 도구로 호출하면 구조화 입력을 받아 docx/pdf/xlsx 바이트를 만든다.

- 순수 pip 의존성만 사용(system 라이브러리 불필요): python-docx / reportlab / openpyxl
- PDF 한글은 reportlab 내장 CID 폰트(HYSMyeongJo-Medium) 사용 → 폰트 파일 번들 불필요
- 입력 마크다운은 최소 문법만 해석(# 제목, - / * 불릿, 1. 번호, 나머지는 문단)
"""
import io
import re

DOCX_CT = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
XLSX_CT = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
PDF_CT = "application/pdf"

_HEADING_RE = re.compile(r"^(#{1,3})\s+(.*)$")
_BULLET_RE = re.compile(r"^\s*[-*]\s+(.*)$")
_NUMBER_RE = re.compile(r"^\s*\d+[.)]\s+(.*)$")
_BOLD_RE = re.compile(r"\*\*([^*]+)\*\*")


def _strip_inline(text: str) -> str:
    """굵게 등 인라인 마크다운 기호 제거(문서에는 평문으로)."""
    return _BOLD_RE.sub(r"\1", text)


def parse_blocks(markdown: str):
    """(kind, text, level) 블록 리스트로 변환. kind: h/p/bullet/number."""
    blocks = []
    for raw in (markdown or "").splitlines():
        line = raw.rstrip()
        if not line.strip():
            continue
        m = _HEADING_RE.match(line)
        if m:
            blocks.append(("h", _strip_inline(m.group(2)), len(m.group(1))))
            continue
        m = _BULLET_RE.match(line)
        if m:
            blocks.append(("bullet", _strip_inline(m.group(1)), 0))
            continue
        m = _NUMBER_RE.match(line)
        if m:
            blocks.append(("number", _strip_inline(m.group(1)), 0))
            continue
        blocks.append(("p", _strip_inline(line), 0))
    return blocks


# ---------------- DOCX ----------------

def render_docx(title: str, markdown: str) -> bytes:
    from docx import Document
    from docx.shared import Pt

    doc = Document()
    if title:
        doc.add_heading(title, level=0)
    for kind, text, level in parse_blocks(markdown):
        if kind == "h":
            doc.add_heading(text, level=min(max(level, 1), 3))
        elif kind == "bullet":
            doc.add_paragraph(text, style="List Bullet")
        elif kind == "number":
            doc.add_paragraph(text, style="List Number")
        else:
            doc.add_paragraph(text)
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


# ---------------- PDF ----------------

_PDF_FONT_REGISTERED = False


def _ensure_pdf_font():
    global _PDF_FONT_REGISTERED
    if _PDF_FONT_REGISTERED:
        return "HYSMyeongJo-Medium"
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont
    pdfmetrics.registerFont(UnicodeCIDFont("HYSMyeongJo-Medium"))
    _PDF_FONT_REGISTERED = True
    return "HYSMyeongJo-Medium"


def render_pdf(title: str, markdown: str) -> bytes:
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, ListFlowable, ListItem
    from xml.sax.saxutils import escape

    font = _ensure_pdf_font()
    styles = getSampleStyleSheet()
    body = ParagraphStyle("body_kr", parent=styles["Normal"], fontName=font, fontSize=11, leading=17)
    h = [ParagraphStyle(f"h{i}_kr", parent=styles["Heading%d" % i], fontName=font,
                        spaceBefore=10, spaceAfter=4) for i in (1, 2, 3)]
    title_style = ParagraphStyle("title_kr", parent=styles["Title"], fontName=font)

    buf = io.BytesIO()
    docp = SimpleDocTemplate(buf, pagesize=A4, topMargin=20 * mm, bottomMargin=20 * mm,
                             leftMargin=18 * mm, rightMargin=18 * mm)
    flow = []
    if title:
        flow.append(Paragraph(escape(title), title_style))
        flow.append(Spacer(1, 6))

    pending_items, pending_kind = [], None

    def flush_list():
        nonlocal pending_items, pending_kind
        if pending_items:
            flow.append(ListFlowable(
                [ListItem(Paragraph(escape(t), body)) for t in pending_items],
                bulletType="bullet" if pending_kind == "bullet" else "1"))
            pending_items, pending_kind = [], None

    for kind, text, level in parse_blocks(markdown):
        if kind in ("bullet", "number"):
            if pending_kind and pending_kind != kind:
                flush_list()
            pending_kind = kind
            pending_items.append(text)
            continue
        flush_list()
        if kind == "h":
            flow.append(Paragraph(escape(text), h[min(max(level, 1), 3) - 1]))
        else:
            flow.append(Paragraph(escape(text), body))
    flush_list()

    docp.build(flow)
    return buf.getvalue()


# ---------------- XLSX ----------------

def render_xlsx(sheets: list) -> bytes:
    """sheets: [{name, headers:[str], rows:[[cell,...]]}]"""
    from openpyxl import Workbook
    from openpyxl.styles import Font

    wb = Workbook()
    wb.remove(wb.active)
    if not sheets:
        sheets = [{"name": "Sheet1", "headers": [], "rows": []}]
    for i, sheet in enumerate(sheets):
        name = str(sheet.get("name") or f"Sheet{i+1}")[:31] or f"Sheet{i+1}"
        ws = wb.create_sheet(title=name)
        headers = sheet.get("headers") or []
        if headers:
            ws.append([str(x) for x in headers])
            for c in ws[1]:
                c.font = Font(bold=True)
        for row in (sheet.get("rows") or []):
            ws.append(["" if v is None else v for v in row])
        # 열 너비 자동(대략)
        for col in ws.columns:
            width = max((len(str(c.value)) if c.value is not None else 0) for c in col)
            ws.column_dimensions[col[0].column_letter].width = min(max(width + 2, 8), 60)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def render(fmt: str, *, title: str = "", markdown: str = "", sheets: list | None = None):
    """포맷별 (bytes, content_type, 확장자) 반환."""
    fmt = (fmt or "").lower()
    if fmt == "docx":
        return render_docx(title, markdown), DOCX_CT, "docx"
    if fmt == "pdf":
        return render_pdf(title, markdown), PDF_CT, "pdf"
    if fmt == "xlsx":
        return render_xlsx(sheets or []), XLSX_CT, "xlsx"
    raise ValueError(f"지원하지 않는 형식: {fmt}")
