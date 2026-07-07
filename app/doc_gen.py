"""문서 생성기 — AI가 도구로 호출하면 구조화 입력을 받아 docx/pdf/xlsx 바이트를 만든다.

- 순수 pip 의존성만 사용(system 라이브러리 불필요): python-docx / reportlab / openpyxl
- PDF 한글은 번들된 Pretendard TTF를 임베딩(뷰어에 폰트 없어도 안 깨짐).
  폰트 파일이 없으면 reportlab 내장 CID 폰트(HYSMyeongJo-Medium)로 폴백.
- 입력 마크다운은 최소 문법만 해석(# 제목, - / * 불릿, 1. 번호, 나머지는 문단)
"""
import io
import os
import re

_FONT_DIR = os.path.join(os.path.dirname(__file__), "assets", "fonts")

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

def _set_docx_korean_font(doc, name: str = "맑은 고딕"):
    """Normal 스타일에 한글(East Asian) 폰트를 명시해 뷰어별 대체 실패로 인한 깨짐을 방지."""
    from docx.oxml.ns import qn
    style = doc.styles["Normal"]
    style.font.name = name
    rpr = style.element.get_or_add_rPr()
    rfonts = rpr.find(qn("w:rFonts"))
    if rfonts is None:
        rfonts = rpr.makeelement(qn("w:rFonts"), {})
        rpr.append(rfonts)
    rfonts.set(qn("w:eastAsia"), name)
    rfonts.set(qn("w:ascii"), name)
    rfonts.set(qn("w:hAnsi"), name)


def render_docx(title: str, markdown: str) -> bytes:
    from docx import Document
    from docx.shared import Pt

    doc = Document()
    _set_docx_korean_font(doc)
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

_PDF_FONTS = {}


def _ensure_pdf_font():
    """한글 TTF(Pretendard)를 PDF에 임베딩해 어떤 뷰어에서도 깨지지 않게 한다.
    폰트 파일이 없으면 내장 CID 폰트로 폴백."""
    global _PDF_FONTS
    if _PDF_FONTS:
        return _PDF_FONTS
    from reportlab.pdfbase import pdfmetrics
    reg = os.path.join(_FONT_DIR, "Pretendard-Regular.ttf")
    bold = os.path.join(_FONT_DIR, "Pretendard-Bold.ttf")
    try:
        if not os.path.isfile(reg):
            raise FileNotFoundError(reg)
        from reportlab.pdfbase.ttfonts import TTFont
        pdfmetrics.registerFont(TTFont("KR", reg))
        pdfmetrics.registerFont(TTFont("KR-Bold", bold if os.path.isfile(bold) else reg))
        pdfmetrics.registerFontFamily("KR", normal="KR", bold="KR-Bold",
                                      italic="KR", boldItalic="KR-Bold")
        _PDF_FONTS = {"regular": "KR", "bold": "KR-Bold"}
    except Exception:
        from reportlab.pdfbase.cidfonts import UnicodeCIDFont
        pdfmetrics.registerFont(UnicodeCIDFont("HYSMyeongJo-Medium"))
        _PDF_FONTS = {"regular": "HYSMyeongJo-Medium", "bold": "HYSMyeongJo-Medium"}
    return _PDF_FONTS


def render_pdf(title: str, markdown: str) -> bytes:
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, ListFlowable, ListItem
    from xml.sax.saxutils import escape

    fonts = _ensure_pdf_font()
    styles = getSampleStyleSheet()
    body = ParagraphStyle("body_kr", parent=styles["Normal"], fontName=fonts["regular"], fontSize=11, leading=17)
    h = [ParagraphStyle(f"h{i}_kr", parent=styles["Heading%d" % i], fontName=fonts["bold"],
                        spaceBefore=10, spaceAfter=4) for i in (1, 2, 3)]
    title_style = ParagraphStyle("title_kr", parent=styles["Title"], fontName=fonts["bold"])

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
