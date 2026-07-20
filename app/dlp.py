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


def _mask_card(m):   return f"****-****-****-{m.group(2)}"
def _mask_rrn(m):    return f"{m.group(1)}-{m.group(2)}******"
def _mask_bizno(m):  return "***-**-*****"
def _mask_phone(m):  return f"{m.group(1)}-****-{m.group(2)}"
def _mask_account(m): return re.sub(r"\d", "*", m.group(0))
def _mask_email(m):  return f"{m.group(1)}***@{m.group(2)}"

_MASKERS = {"card": _mask_card, "rrn": _mask_rrn, "bizno": _mask_bizno,
            "phone": _mask_phone, "account": _mask_account, "email": _mask_email}


def enabled_categories() -> list[str]:
    raw = (settings.dlp_categories or "").lower()
    picked = {c.strip() for c in raw.split(",") if c.strip()}
    return [c for c in _ORDER if c in picked and c in _PATTERNS]


def mask_text(text, cats=None):
    """민감정보를 마스킹한 문자열과 카테고리별 적중 횟수(Counter)를 반환."""
    if not isinstance(text, str) or not text:
        return text, Counter()
    cats = cats if cats is not None else enabled_categories()
    counts = Counter()
    for cat in cats:
        pat, masker = _PATTERNS[cat], _MASKERS[cat]

        def _sub(m, _cat=cat, _mk=masker):
            counts[_cat] += 1
            return _mk(m)

        text = pat.sub(_sub, text)
    return text, counts


def scrub_text(text):
    """설정 카테고리로 마스킹한 문자열만 반환(비활성/비문자면 원본)."""
    if not isinstance(text, str):
        return text
    return mask_text(text)[0]


def scrub_messages(messages):
    """대화 메시지 리스트를 새 리스트로 복사하며 content를 마스킹.
    content가 문자열이면 그대로, 블록 리스트면 각 text 블록을 마스킹(원본 불변)."""
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
            blocks = []
            for b in c:
                if isinstance(b, dict) and isinstance(b.get("text"), str):
                    blocks.append({**b, "text": mask_text(b["text"], cats)[0]})
                else:
                    blocks.append(b)
            out.append({**m, "content": blocks})
        else:
            out.append(m)
    return out


def scrub_params(params: dict):
    """스킬 파라미터 값(문자열)을 마스킹한 새 dict 반환."""
    cats = enabled_categories()
    if not cats or not isinstance(params, dict):
        return params
    return {k: (mask_text(v, cats)[0] if isinstance(v, str) else v) for k, v in params.items()}


def _company_host_patterns() -> list[str]:
    pats = []
    for raw in (settings.skill_internal_allowed_hosts or "").split(","):
        item = raw.strip().lower()
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
