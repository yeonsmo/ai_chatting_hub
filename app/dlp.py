"""DLP(Data Loss Prevention) — 나가는 내용에서 민감정보를 검사·마스킹한다.

두 곳에 적용한다(각각 설정으로 on/off):
  1) 모델 송신(dlp_model_egress): 외부 AI(가비아 등)로 보내는 시스템프롬프트·대화 내용.
     주의: 켜면 AI가 실제 번호를 못 보므로 그 값이 들어가는 서식 자동작성이 마스킹된다.
  2) 스킬 송신(dlp_skill_egress): 회사 도메인이 아닌 '외부' API로 보내는 파라미터 값.
     회사 자체 시스템(사내 허용목록 호스트)에는 실제 값을 그대로 보낸다.

카테고리: rrn(주민등록번호) · card(카드번호) · bizno(사업자등록번호) ·
          account(계좌번호, 하이픈형 휴리스틱) · phone(휴대전화) · email(이메일)
"""
import re
from collections import Counter

from app.config import settings

# 마스킹 순서 주의: account 규칙이 넓어 bizno/phone/rrn을 삼킬 수 있으므로 그 뒤에 둔다.
_ORDER = ["card", "rrn", "bizno", "phone", "account", "email"]

_PATTERNS = {
    "card":    re.compile(r"(?<!\d)(\d{4})[-\s]?\d{4}[-\s]?\d{4}[-\s]?(\d{4})(?!\d)"),
    "rrn":     re.compile(r"(?<!\d)(\d{6})[-\s]?([1-4])\d{6}(?!\d)"),
    "bizno":   re.compile(r"(?<!\d)\d{3}-\d{2}-\d{5}(?!\d)"),
    "phone":   re.compile(r"(?<!\d)(01[016789])[-\s]?\d{3,4}[-\s]?(\d{4})(?!\d)"),
    "account": re.compile(r"(?<!\d)\d{2,6}-\d{2,6}-\d{2,7}(?:-\d{1,3})?(?!\d)"),
    "email":   re.compile(r"\b([\w.+-])[\w.+-]*@([\w.-]+\.\w+)\b"),
}


def _luhn_ok(digits: str) -> bool:
    """카드번호 Luhn 체크섬 검증(오탐 감소용). 실패하면 카드가 아니라고 본다."""
    total, alt = 0, False
    for ch in reversed(digits):
        d = ord(ch) - 48
        if alt:
            d *= 2
            if d > 9:
                d -= 9
        total += d
        alt = not alt
    return total % 10 == 0


def _mask_card(m):
    # 16자리 숫자가 Luhn 체크에 맞을 때만 카드로 간주(연속 숫자·일반 코드 오탐 방지).
    digits = re.sub(r"\D", "", m.group(0))
    if len(digits) != 16 or not _luhn_ok(digits):
        return None
    return f"****-****-****-{m.group(2)}"


def _mask_rrn(m):    return f"{m.group(1)}-{m.group(2)}******"
def _mask_bizno(m):  return "***-**-*****"
def _mask_phone(m):  return f"{m.group(1)}-****-{m.group(2)}"


def _mask_account(m):
    # 계좌번호는 최소 10자리 이상의 숫자로만 인정 → 날짜(2024-01-15=8자리) 등 오탐 방지.
    digits = re.sub(r"\D", "", m.group(0))
    if len(digits) < 10:
        return None
    return re.sub(r"\d", "*", m.group(0))


def _mask_email(m):  return f"{m.group(1)}***@{m.group(2)}"

_MASKERS = {"card": _mask_card, "rrn": _mask_rrn, "bizno": _mask_bizno,
            "phone": _mask_phone, "account": _mask_account, "email": _mask_email}


def enabled_categories() -> list[str]:
    raw = (settings.dlp_categories or "").lower()
    picked = {c.strip() for c in raw.split(",") if c.strip()}
    return [c for c in _ORDER if c in picked and c in _PATTERNS]


def mask_text(text, cats=None):
    """민감정보를 마스킹한 문자열과 카테고리별 적중 횟수(Counter)를 반환.
    마스커가 None을 반환하면(오탐 판정) 원문 유지 + 카운트 미증가."""
    if not isinstance(text, str) or not text:
        return text, Counter()
    cats = cats if cats is not None else enabled_categories()
    counts = Counter()
    for cat in cats:
        pat, masker = _PATTERNS[cat], _MASKERS[cat]

        def _sub(m, _cat=cat, _mk=masker):
            replaced = _mk(m)
            if replaced is None:          # 오탐 → 원문 그대로 두고 카운트하지 않음
                return m.group(0)
            counts[_cat] += 1
            return replaced

        text = pat.sub(_sub, text)
    return text, counts


def scrub_text(text):
    """설정 카테고리로 마스킹한 문자열만 반환(비활성/비문자면 원본)."""
    if not isinstance(text, str):
        return text
    return mask_text(text)[0]


def _scrub_value(value, cats):
    """문자열/딕트/리스트를 재귀적으로 마스킹(불변: 새 객체 반환)."""
    if isinstance(value, str):
        return mask_text(value, cats)[0]
    if isinstance(value, dict):
        return {k: _scrub_value(v, cats) for k, v in value.items()}
    if isinstance(value, list):
        return [_scrub_value(v, cats) for v in value]
    return value


def scrub_messages(messages):
    """대화 메시지 리스트를 새 리스트로 복사하며 content를 마스킹.
    content가 문자열이면 그대로, 블록 리스트면 각 블록을 마스킹한다.
    text 블록의 text, tool_use 블록의 input(중첩 포함), tool_result 블록의 content를 모두 다룬다."""
    cats = enabled_categories()
    if not cats or not isinstance(messages, list):
        return messages
    out = []
    for m in messages:
        if not isinstance(m, dict):
            out.append(m); continue
        c = m.get("content")
        if isinstance(c, str):
            out.append({**m, "content": mask_text(c, cats)[0]})
        elif isinstance(c, list):
            out.append({**m, "content": [_scrub_block(b, cats) for b in c]})
        else:
            out.append(m)
    return out


def _scrub_block(b, cats):
    """단일 content 블록(dict) 마스킹. tool_use.input, tool_result.content, text 등."""
    if not isinstance(b, dict):
        return b
    btype = b.get("type")
    if btype == "tool_use" and "input" in b:
        # 모델이 스킬/도구에 넘기는 인자(계좌·주민번호 등)까지 마스킹
        return {**b, "input": _scrub_value(b.get("input"), cats)}
    if btype == "tool_result" and "content" in b:
        # tool_result.content 는 문자열이거나 블록 리스트일 수 있다
        return {**b, "content": _scrub_value(b.get("content"), cats)}
    if isinstance(b.get("text"), str):
        return {**b, "text": mask_text(b["text"], cats)[0]}
    return b


def scrub_content_blocks(blocks):
    """content 블록 리스트만 단독으로 마스킹(프로바이더 도구 루프에서 사용)."""
    cats = enabled_categories()
    if not cats or not isinstance(blocks, list):
        return blocks
    return [_scrub_block(b, cats) for b in blocks]


def scrub_openai_messages(messages):
    """OpenAI 포맷 메시지 마스킹. content(문자열/멀티모달 리스트)와
    assistant tool_calls[].function.arguments, tool 역할 메시지 content를 다룬다."""
    cats = enabled_categories()
    if not cats or not isinstance(messages, list):
        return messages
    out = []
    for m in messages:
        if not isinstance(m, dict):
            out.append(m); continue
        nm = dict(m)
        c = m.get("content")
        if isinstance(c, str):
            nm["content"] = mask_text(c, cats)[0]
        elif isinstance(c, list):
            nm["content"] = [_scrub_openai_part(p, cats) for p in c]
        tcs = m.get("tool_calls")
        if isinstance(tcs, list):
            nm["tool_calls"] = [_scrub_openai_toolcall(t, cats) for t in tcs]
        out.append(nm)
    return out


def _scrub_openai_part(p, cats):
    if isinstance(p, dict) and isinstance(p.get("text"), str):
        return {**p, "text": mask_text(p["text"], cats)[0]}
    return p


def _scrub_openai_toolcall(t, cats):
    if not isinstance(t, dict):
        return t
    fn = t.get("function")
    if isinstance(fn, dict) and isinstance(fn.get("arguments"), str):
        return {**t, "function": {**fn, "arguments": mask_text(fn["arguments"], cats)[0]}}
    return t


def scrub_params(params):
    """스킬 파라미터 값을 재귀적으로 마스킹한 새 dict 반환(중첩 dict/list 포함)."""
    cats = enabled_categories()
    if not cats or not isinstance(params, dict):
        return params
    return {k: _scrub_value(v, cats) for k, v in params.items()}


def _company_host_patterns() -> list[str]:
    # DLP 전용 회사 도메인 목록이 있으면 그것을, 없으면 SSRF 허용목록을 재사용.
    raw = settings.dlp_company_hosts or settings.skill_internal_allowed_hosts or ""
    pats = []
    for item in raw.split(","):
        item = item.strip().lower()
        if item and "/" not in item:      # CIDR은 호스트명 매칭 대상 아님
            pats.append(item)
    return pats


def is_company_host(host: str) -> bool:
    """호스트가 사내 허용목록(회사 도메인)에 해당하면 True → 마스킹 제외(실제 값 전송)."""
    h = (host or "").lower().rstrip(".")
    if not h:
        return False
    for pat in _company_host_patterns():
        if pat.startswith("*."):
            base = pat[2:]
            if h == base or h.endswith("." + base):
                return True
        elif h == pat:
            return True
    return False
