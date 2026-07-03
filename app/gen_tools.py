"""AI가 대화 중 호출하는 내장 생성 도구(문서 생성). 스킬(외부 HTTP)과 별개.

각 도구는 (파일 바이트, content_type, 확장자, 파일명)을 만들고, chat.py의 executor가
이를 생성물 Attachment로 저장한다.
"""
from app import doc_gen

# 안전한 파일명(확장자 제외) 정리
import re
_SAFE = re.compile(r"[^0-9A-Za-z가-힣 _\-.]")


def safe_filename(name: str, default: str, ext: str) -> str:
    base = _SAFE.sub("", (name or "").strip()) or default
    base = base[:60].rstrip(". ")
    if base.lower().endswith("." + ext):
        base = base[: -(len(ext) + 1)]
    return f"{base or default}.{ext}"


CREATE_DOCUMENT = {
    "name": "create_document",
    "description": (
        "사용자에게 내려받을 Word(docx) 또는 PDF 문서를 생성한다. 보고서·안내문·정리 문서 등 "
        "서술형 문서가 필요할 때 사용. content는 마크다운(# 제목, - 불릿, 1. 번호, **굵게**)을 지원."
    ),
    "params": {
        "type": "object",
        "properties": {
            "format": {"type": "string", "enum": ["docx", "pdf"], "description": "문서 형식"},
            "filename": {"type": "string", "description": "확장자 없는 파일 이름"},
            "title": {"type": "string", "description": "문서 제목"},
            "content": {"type": "string", "description": "마크다운 본문"},
        },
        "required": ["format", "title", "content"],
    },
}

CREATE_SPREADSHEET = {
    "name": "create_spreadsheet",
    "description": (
        "사용자에게 내려받을 Excel(xlsx) 파일을 생성한다. 표·목록·집계 데이터가 필요할 때 사용. "
        "여러 시트를 만들 수 있다."
    ),
    "params": {
        "type": "object",
        "properties": {
            "filename": {"type": "string", "description": "확장자 없는 파일 이름"},
            "sheets": {
                "type": "array",
                "description": "시트 배열",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "description": "시트 이름"},
                        "headers": {"type": "array", "items": {"type": "string"}, "description": "머리글 행"},
                        "rows": {"type": "array", "items": {"type": "array"}, "description": "데이터 행(2차원)"},
                    },
                    "required": ["name"],
                },
            },
        },
        "required": ["sheets"],
    },
}

TOOLS = {CREATE_DOCUMENT["name"]: CREATE_DOCUMENT, CREATE_SPREADSHEET["name"]: CREATE_SPREADSHEET}


def anthropic_defs() -> list:
    return [{"name": t["name"], "description": t["description"], "input_schema": t["params"]}
            for t in TOOLS.values()]


def openai_defs() -> list:
    return [{"type": "function", "function": {
        "name": t["name"], "description": t["description"], "parameters": t["params"]}}
        for t in TOOLS.values()]


def run_tool(name: str, params: dict):
    """(bytes, content_type, ext, filename) 반환. 알 수 없는 도구면 ValueError."""
    if name == "create_document":
        fmt = (params.get("format") or "docx").lower()
        if fmt not in ("docx", "pdf"):
            fmt = "docx"
        data, ct, ext = doc_gen.render(fmt, title=str(params.get("title") or ""),
                                       markdown=str(params.get("content") or ""))
        fn = safe_filename(params.get("filename") or params.get("title"), "문서", ext)
        return data, ct, ext, fn
    if name == "create_spreadsheet":
        sheets = params.get("sheets") or []
        if not isinstance(sheets, list):
            raise ValueError("sheets는 배열이어야 합니다")
        data, ct, ext = doc_gen.render("xlsx", sheets=sheets)
        fn = safe_filename(params.get("filename"), "표", ext)
        return data, ct, ext, fn
    raise ValueError(f"알 수 없는 생성 도구: {name}")
