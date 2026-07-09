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

CREATE_MEETING_MINUTES = {
    "name": "create_meeting_minutes",
    "description": (
        "회사 표준 회의록 양식(HP-QP-750-03)에 맞춰 완성된 PDF 회의록을 생성한다. "
        "사용자가 회의 메모/녹취를 주고 회의록 작성을 요청하면 이 도구를 사용한다. "
        "회의 내용을 분석해 아래 항목을 채우되, 메모에 없는 항목은 비워 둔다(지어내지 말 것). "
        "content(회의내용)는 핵심을 번호매김/문단으로 정리하고, notes(특기사항)에는 결정사항·"
        "액션아이템·후속조치를 항목별로 최대 9개까지 넣는다."
    ),
    "params": {
        "type": "object",
        "properties": {
            "datetime": {"type": "string", "description": "회의 일시 (예: 2026-07-08 14:00)"},
            "place": {"type": "string", "description": "회의 장소"},
            "dept": {"type": "string", "description": "작성부서"},
            "writer": {"type": "string", "description": "작성자"},
            "ext_company": {"type": "string", "description": "외부업체명(있을 때만)"},
            "inside": {"type": "array", "items": {"type": "string"},
                       "description": "내부 참석자 이름 목록(최대 3명 표기)"},
            "outside": {"type": "array", "items": {"type": "string"},
                        "description": "외부 참석자 이름 목록(최대 5명 표기)"},
            "purpose": {"type": "string", "description": "회의목적(한 줄 요약)"},
            "content": {"type": "string", "description": "회의내용 본문. 번호/문단으로 정리, 줄바꿈은 \\n"},
            "notes": {"type": "array", "items": {"type": "string"},
                      "description": "특기사항(결정사항·액션아이템·후속조치) 최대 9개"},
            "mgmt_no": {"type": "string", "description": "관리번호(있을 때만)"},
            "approval_no": {"type": "string", "description": "승인번호(있을 때만)"},
            "filename": {"type": "string", "description": "확장자 없는 파일 이름"},
        },
        "required": ["content"],
    },
}

_ITEM_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string", "description": "품명/항목(거래명세서는 '일자 품명'도 가능)"},
        "unit": {"type": "string", "description": "단위(식/부/EA/개 등)"},
        "qty": {"type": "number", "description": "수량"},
        "price": {"type": "number", "description": "단가(원)"},
        "amount": {"type": "number", "description": "공급가액. 생략하면 수량×단가로 자동계산"},
        "note": {"type": "string", "description": "비고(선택)"},
    },
    "required": ["name"],
}

CREATE_ESTIMATE = {
    "name": "create_estimate",
    "description": (
        "HP엔지니어링 표준 '견적서'(회사 양식) PDF를 생성한다. 사용자가 견적서 작성을 요청하면 "
        "대화하며 거래처·품목·단가 등을 함께 정리한 뒤 이 도구를 호출한다. 공급가액·세액(10%)·합계는 "
        "수량과 단가로 자동계산되므로 직접 계산해 넣지 말 것. 아는 값만 채우고 모르는 항목은 비워 둔다"
        "(지어내지 말 것). 간소화된 양식이 필요하면 simple=true(간이견적서)."
    ),
    "params": {
        "type": "object",
        "properties": {
            "simple": {"type": "boolean", "description": "간이견적서 양식이면 true(기본 false=정식 견적서)"},
            "client_name": {"type": "string", "description": "거래처(회사) 이름"},
            "client_manager": {"type": "string", "description": "거래처 담당자 성함"},
            "client_contact": {"type": "string", "description": "거래처 담당자 연락처"},
            "client_fax": {"type": "string", "description": "거래처 팩스번호"},
            "estimate_name": {"type": "string", "description": "견적 이름/건명"},
            "target_spec": {"type": "string", "description": "시험 대상품목 명 또는 규격(규정)"},
            "hp_manager": {"type": "string", "description": "HP엔지니어링 담당자 성함"},
            "hp_phone": {"type": "string", "description": "HP엔지니어링 담당자 사내 직통번호"},
            "hp_email": {"type": "string", "description": "HP엔지니어링 담당자 이메일"},
            "estimate_no": {"type": "string", "description": "견적번호(있을 때만)"},
            "issue_date": {"type": "string", "description": "견적서 발행일(예: 2026-07-09)"},
            "items": {"type": "array", "items": _ITEM_SCHEMA, "description": "견적 품목(최대 11개)"},
            "work_deadline": {"type": "string", "description": "작업완료 기한"},
            "delivery_place": {"type": "string", "description": "인도장소"},
            "confirm_notes": {"type": "string", "description": "확인/협의 사항"},
            "payment_date": {"type": "string", "description": "대금 결제일"},
            "filename": {"type": "string", "description": "확장자 없는 파일 이름"},
        },
        "required": ["client_name", "items"],
    },
}

CREATE_TRANSACTION_STATEMENT = {
    "name": "create_transaction_statement",
    "description": (
        "HP엔지니어링 표준 '거래명세서'(회사 양식) PDF를 생성한다. 사용자가 거래명세서 작성을 요청하면 "
        "대화하며 거래처·작업명·품목 등을 정리한 뒤 이 도구를 호출한다. 공급가액·세액(10%)·합계는 "
        "수량과 단가로 자동계산되므로 직접 계산하지 말 것. 아는 값만 채우고 모르는 항목은 비워 둔다."
    ),
    "params": {
        "type": "object",
        "properties": {
            "client_name": {"type": "string", "description": "거래처(회사) 이름"},
            "client_manager": {"type": "string", "description": "거래처 담당자 이름"},
            "work_name": {"type": "string", "description": "작업명(시험명)"},
            "statement_no": {"type": "string", "description": "거래명세번호(있을 때만)"},
            "items": {"type": "array", "items": _ITEM_SCHEMA,
                      "description": "거래 품목(최대 15개). name에 '일자 품명'을 함께 적을 수 있음"},
            "work_done": {"type": "string", "description": "작업완료 현황/시기"},
            "delivery_done": {"type": "string", "description": "인도완료 일자/현황"},
            "attach_date": {"type": "string", "description": "거래명세서 첨부일"},
            "payment_terms": {"type": "string", "description": "대금지불 조건(예: 익월 20일 지급)"},
            "filename": {"type": "string", "description": "확장자 없는 파일 이름"},
        },
        "required": ["client_name", "items"],
    },
}

CREATE_OVERTIME_REQUEST = {
    "name": "create_overtime_request",
    "description": (
        "HP엔지니어링 표준 '연장 근로 신청서'(회사 양식) PDF를 생성한다. 직원이 야근/연장근무 신청서를 "
        "요청하면 대화하며 신청 정보를 정리한 뒤 이 도구를 호출한다. 총 연장근로시간은 시작/종료 시간으로 "
        "자동계산되므로 직접 계산해 넣지 말 것. 부서는 정해진 목록에서만 선택. 아는 값만 채운다."
    ),
    "params": {
        "type": "object",
        "properties": {
            "apply_date": {"type": "string", "description": "신청일자(예: 2026-07-09)"},
            "applicant": {"type": "string", "description": "신청자 성명"},
            "hire_date": {"type": "string", "description": "입사일"},
            "dept": {"type": "string", "enum": ["경영지원", "기술엔지니어링", "연구개발전담부서"],
                     "description": "부서"},
            "position": {"type": "string", "description": "직위"},
            "emp_no": {"type": "string", "description": "사번"},
            "start_time": {"type": "string", "description": "연장근무 시작시간(예: 18:30)"},
            "end_time": {"type": "string", "description": "연장근무 종료시간(예: 21:00)"},
            "total_hours": {"type": "string", "description": "총 연장근로시간(비우면 시작/종료로 자동계산)"},
            "reason": {"type": "string", "description": "연장근무 사유"},
            "remarks": {"type": "string", "description": "특이사항"},
            "mgmt_no": {"type": "string", "description": "관리번호(있을 때만)"},
            "approval_no": {"type": "string", "description": "승인번호(있을 때만)"},
            "writer": {"type": "string", "description": "작성자 성명(비우면 신청자와 동일)"},
            "filename": {"type": "string", "description": "확장자 없는 파일 이름"},
        },
        "required": ["applicant", "start_time", "end_time", "reason"],
    },
}

_EXPENSE_ITEM_SCHEMA = {
    "type": "object",
    "properties": {
        "item": {"type": "string", "description": "지출 항목명"},
        "vendor": {"type": "string", "description": "거래처(구입처/지출처)"},
        "memo": {"type": "string", "description": "적요/기타사항/계정과목"},
        "amount": {"type": "number", "description": "지출 금액(원)"},
        "proof": {"type": "string",
                  "enum": ["영수증", "세금계산서", "간이영수증", "카드영수증", "현금영수증", "기타", "분실"],
                  "description": "증빙 구분"},
        "project": {"type": "string", "description": "관련 프로젝트(선택)"},
        "note": {"type": "string", "description": "비고(선택)"},
    },
    "required": ["item"],
}

CREATE_EXPENSE_REPORT = {
    "name": "create_expense_report",
    "description": (
        "HP엔지니어링 표준 '지출결의서'(회사 양식) PDF를 생성한다. 직원이 경비/지출 결의서를 요청하면 "
        "대화하며 지출 항목을 정리한 뒤 이 도구를 호출한다. 금액 합계는 자동계산되므로 직접 계산하지 말 것. "
        "증빙구분은 정해진 목록에서만 선택. 아는 값만 채우고 없는 값은 비운다."
    ),
    "params": {
        "type": "object",
        "properties": {
            "writer_name": {"type": "string", "description": "작성자 성명"},
            "writer_rank": {"type": "string", "description": "작성자 직급"},
            "writer_empno": {"type": "string", "description": "작성자 사번"},
            "draft_date": {"type": "string", "description": "기안일자"},
            "title": {"type": "string", "description": "지출 제목"},
            "reason": {"type": "string", "description": "지출 사유"},
            "items": {"type": "array", "items": _EXPENSE_ITEM_SCHEMA, "description": "지출 항목(최대 8개)"},
            "pay_method": {"type": "string", "enum": ["계좌이체", "현금", "법인카드"], "description": "지급방법"},
            "bank": {"type": "string", "description": "수령 계좌 은행"},
            "account": {"type": "string", "description": "수령 계좌번호"},
            "special_notes": {"type": "string", "description": "특기사항(증빙이 기타/분실이면 사유 필수)"},
            "mgmt_no": {"type": "string", "description": "관리번호(있을 때만)"},
            "approval_no": {"type": "string", "description": "승인번호(있을 때만)"},
            "filename": {"type": "string", "description": "확장자 없는 파일 이름"},
        },
        "required": ["writer_name", "title", "items"],
    },
}

TOOLS = {CREATE_DOCUMENT["name"]: CREATE_DOCUMENT,
         CREATE_SPREADSHEET["name"]: CREATE_SPREADSHEET,
         CREATE_MEETING_MINUTES["name"]: CREATE_MEETING_MINUTES,
         CREATE_ESTIMATE["name"]: CREATE_ESTIMATE,
         CREATE_TRANSACTION_STATEMENT["name"]: CREATE_TRANSACTION_STATEMENT,
         CREATE_OVERTIME_REQUEST["name"]: CREATE_OVERTIME_REQUEST,
         CREATE_EXPENSE_REPORT["name"]: CREATE_EXPENSE_REPORT}

# 실행이 async/DB가 필요해 run_tool로 처리하지 않고 chat.py에서 직접 실행하는 도구.
# 스키마(정의)만 여기서 노출한다.
GENERATE_IMAGE = {
    "name": "generate_image",
    "description": (
        "사용자가 이미지·그림·삽화·일러스트·로고 등 '이미지 생성'을 요청할 때 호출한다. "
        "현재 어떤 모델과 대화 중이든 이 도구로 이미지를 만들 수 있으므로, 사용자가 모델을 "
        "따로 바꾸지 않아도 된다. 생성된 이미지는 사용자 대화 화면에 바로 표시·다운로드된다."
    ),
    "params": {
        "type": "object",
        "properties": {
            "prompt": {"type": "string", "description": "생성할 이미지에 대한 구체적 묘사(영문/한글 무관)"},
        },
        "required": ["prompt"],
    },
}

# 도구 정의 노출용(run_tool 대상 + async 직접 실행 대상)
_ALL_DEFS = list(TOOLS.values()) + [GENERATE_IMAGE]


def anthropic_defs() -> list:
    return [{"name": t["name"], "description": t["description"], "input_schema": t["params"]}
            for t in _ALL_DEFS]


def openai_defs() -> list:
    return [{"type": "function", "function": {
        "name": t["name"], "description": t["description"], "parameters": t["params"]}}
        for t in _ALL_DEFS]


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
    if name == "create_meeting_minutes":
        data = doc_gen.render_minutes_pdf(params or {})
        fn = safe_filename(params.get("filename") or params.get("purpose"), "회의록", "pdf")
        return data, doc_gen.PDF_CT, "pdf", fn
    if name == "create_estimate":
        data = doc_gen.render_estimate_pdf(params or {})
        default = "간이견적서" if (params or {}).get("simple") else "견적서"
        fn = safe_filename(params.get("filename") or params.get("estimate_name")
                           or params.get("client_name"), default, "pdf")
        return data, doc_gen.PDF_CT, "pdf", fn
    if name == "create_transaction_statement":
        data = doc_gen.render_transaction_statement_pdf(params or {})
        fn = safe_filename(params.get("filename") or params.get("work_name")
                           or params.get("client_name"), "거래명세서", "pdf")
        return data, doc_gen.PDF_CT, "pdf", fn
    if name == "create_overtime_request":
        data = doc_gen.render_overtime_request_pdf(params or {})
        fn = safe_filename(params.get("filename") or params.get("applicant"), "연장근로신청서", "pdf")
        return data, doc_gen.PDF_CT, "pdf", fn
    if name == "create_expense_report":
        data = doc_gen.render_expense_report_pdf(params or {})
        fn = safe_filename(params.get("filename") or params.get("title")
                           or params.get("writer_name"), "지출결의서", "pdf")
        return data, doc_gen.PDF_CT, "pdf", fn
    raise ValueError(f"알 수 없는 생성 도구: {name}")
