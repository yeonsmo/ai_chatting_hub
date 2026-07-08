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


# ---------------- 회의록 (HP 표준 양식 오버레이) ----------------

_FORM_DIR = os.path.join(os.path.dirname(__file__), "assets", "forms")
_MINUTES_TEMPLATE = os.path.join(_FORM_DIR, "HP-QP-750-03_meeting.pdf")
_PAGE_H = 842.0  # A4 pt


def _mt_wrap(text: str, font: str, size: float, maxw: float) -> list:
    """폭 기준 줄바꿈(한글은 글자 단위). 명시적 개행도 처리."""
    from reportlab.pdfbase.pdfmetrics import stringWidth
    out, line = [], ""
    for ch in str(text or ""):
        if ch == "\n":
            out.append(line); line = ""; continue
        if stringWidth(line + ch, font, size) > maxw and line:
            out.append(line); line = ch
        else:
            line += ch
    if line:
        out.append(line)
    return out


def _as_str_list(v) -> list:
    """모델이 배열 대신 dict/숫자/문자열로 넘겨도 안전하게 문자열 리스트로 정규화."""
    if v is None:
        return []
    if isinstance(v, list):
        return [str(x) for x in v if x is not None and str(x).strip()]
    if isinstance(v, dict):
        return [str(x) for x in v.values() if x is not None and str(x).strip()]
    s = str(v).strip()
    return [s] if s else []


def _as_names(v) -> list:
    """참석자 이름 리스트로 정규화(문자열이면 쉼표/공백 분리)."""
    if isinstance(v, str):
        return [s.strip() for s in v.replace(",", " ").split() if s.strip()]
    return _as_str_list(v)


# 정적 양식은 런타임에 바뀌지 않으므로 바이트를 한 번만 읽어 캐시(반복 생성 시 디스크 I/O 절감).
_MINUTES_TEMPLATE_BYTES = None


def _minutes_template_bytes() -> bytes:
    global _MINUTES_TEMPLATE_BYTES
    if _MINUTES_TEMPLATE_BYTES is None:
        with open(_MINUTES_TEMPLATE, "rb") as f:
            _MINUTES_TEMPLATE_BYTES = f.read()
    return _MINUTES_TEMPLATE_BYTES


def render_minutes_pdf(fields: dict) -> bytes:
    """HP 표준 회의록 양식(HP-QP-750-03) 위에 값을 오버레이해 PDF 생성.
    양식은 정적 PDF(폼 필드 없음)이므로 좌표 기반으로 텍스트를 그려 원본 레이아웃을 그대로 유지한다."""
    import io as _io
    from reportlab.pdfgen import canvas
    from pypdf import PdfReader, PdfWriter

    fonts = _ensure_pdf_font()
    reg = fonts["regular"]
    buf = _io.BytesIO()
    c = canvas.Canvas(buf, pagesize=(595, 842))
    c.setFillColorRGB(0.09, 0.09, 0.11)

    def line(text, x, ybase, size=9, center=None):
        if text is None or text == "":
            return
        c.setFont(reg, size)
        if center is not None:
            c.drawCentredString(center, _PAGE_H - ybase, str(text))
        else:
            c.drawString(x, _PAGE_H - ybase, str(text))

    def block(text, x, ytop, maxw, maxlines, size=9.5, lh=14):
        if text is None or text == "":
            return
        c.setFont(reg, size)
        lines = _mt_wrap(text, reg, size, maxw)
        if len(lines) > maxlines:  # 1페이지 양식 한계 초과 시 잘림을 명시(무단 누락 방지)
            lines = lines[:maxlines]
            lines[-1] = lines[-1][:max(0, len(lines[-1]) - 6)] + " …(이하 생략)"
        y = ytop
        for ln in lines:
            c.drawString(x, _PAGE_H - y, ln); y += lh

    g = fields.get
    line(g("mgmt_no"), 75, 133, 8.5)
    line(g("approval_no"), 360, 133, 8.5)
    line(g("datetime"), 125, 155)
    line(g("dept"), 326, 155)
    line(g("writer"), 486, 155)
    line(g("place"), 125, 178)
    line(g("ext_company"), 326, 178)
    inside = _as_names(g("inside"))
    outside = _as_names(g("outside"))
    for i, cx in enumerate([143, 190, 238]):
        if i < len(inside):
            line(inside[i], 0, 201, 8, center=cx)
    for i, cx in enumerate([353, 409, 457, 504, 552]):
        if i < len(outside):
            line(outside[i], 0, 201, 8, center=cx)
    line(g("purpose"), 125, 225)
    block(g("content"), 125, 246, 446, 18, size=9.5, lh=14)
    tops = [513, 542, 572, 601, 631, 660, 690, 719, 749]
    notes = _as_str_list(g("notes"))
    for i, item in enumerate(notes[:9]):
        block(item, 124, tops[i], 447, 2, size=9, lh=13.5)
    c.save(); buf.seek(0)

    overlay = PdfReader(buf).pages[0]
    tmpl = PdfReader(_io.BytesIO(_minutes_template_bytes()))
    page = tmpl.pages[0]
    page.merge_page(overlay)
    writer = PdfWriter(); writer.add_page(page)
    out = _io.BytesIO(); writer.write(out)
    return out.getvalue()


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
