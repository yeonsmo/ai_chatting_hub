"""HR 모듈 연동 — 직원 명부 동기화(방향 ①: HR → 허브).

HR 서버의 REST API(`GET /employees`)를 호출해 허브 계정에 사번·부서·직급을 채우고,
재직상태(active/leave/resigned)에 따라 로그인 활성/비활성을 맞춘다.

- 매칭 키: 사번(employee_no). 없으면 이메일로 폴백 매칭.
- 재직: 계정 정보 갱신(+없으면 hr_auto_create 시 생성).
- 휴직/퇴사: is_active=False + token_version 증가(기존 세션 폐기). 데이터는 보존.
- 최고관리자 계정은 절대 비활성/변경하지 않는다(안전장치).
- 필드명은 명세서 기준이되, 흔한 변형(department/부서 등)도 관대하게 인식.
"""
import json
import secrets
from urllib.parse import quote

import httpx
from passlib.context import CryptContext
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import User, UserRole

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
REQUEST_TIMEOUT = 30.0


class HRSyncError(Exception):
    """연동 설정 누락·통신 실패 등 동기화를 진행할 수 없는 상황."""


def hr_configured() -> bool:
    return bool((settings.hr_base_url or "").strip() and (settings.hr_api_key or "").strip())


def _auth_headers() -> dict:
    key = (settings.hr_api_key or "").strip()
    if not key:
        raise HRSyncError("HR API 키가 설정되지 않았습니다 (.env HR_API_KEY).")
    header = (settings.hr_auth_header or "Authorization").strip()
    prefix = settings.hr_auth_prefix if settings.hr_auth_prefix is not None else "Bearer "
    if prefix and not prefix.endswith(" "):   # "Bearer" → "Bearer " (공백 누락 허용)
        prefix += " "
    return {header: f"{prefix}{key}"}


def _g(d: dict, *keys, default=None):
    for k in keys:
        v = d.get(k) if isinstance(d, dict) else None
        if v not in (None, ""):
            return v
    return default


def _norm_status(s) -> str:
    s = str(s or "").strip().lower()
    if s in ("active", "재직", "employed", "y", "true", "1"):
        return "active"
    if s in ("resigned", "퇴사", "terminated", "left", "退社"):
        return "resigned"
    if s in ("leave", "휴직", "suspended", "on_leave"):
        return "leave"
    return s or "active"


async def fetch_employees(updated_since: str | None = None) -> list[dict]:
    """HR 명부 전체 조회(페이지네이션 순회). 실패 시 HRSyncError."""
    base = (settings.hr_base_url or "").strip().rstrip("/")
    if not base:
        raise HRSyncError("HR 서버 주소가 설정되지 않았습니다 (.env HR_BASE_URL).")
    path = settings.hr_employees_path or "/api/v1/employees"
    size = max(1, min(int(settings.hr_page_size or 100), 500))
    out: list[dict] = []
    try:
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
            page = 1
            while page <= 500:  # 안전 상한
                params = {"page": page, "size": size}
                if updated_since:
                    params["updated_since"] = updated_since
                r = await client.get(base + path, params=params, headers=_auth_headers())
                r.raise_for_status()
                data = r.json()
                emps = data.get("employees") if isinstance(data, dict) else data
                emps = emps or []
                out.extend([e for e in emps if isinstance(e, dict)])
                total = data.get("total") if isinstance(data, dict) else None
                if len(emps) < size or (total is not None and len(out) >= total):
                    break
                page += 1
    except httpx.HTTPStatusError as e:
        raise HRSyncError(f"HR 서버 응답 오류 (HTTP {e.response.status_code})")
    except httpx.HTTPError as e:
        raise HRSyncError(f"HR 서버에 연결할 수 없습니다 ({type(e).__name__})")
    except (ValueError, KeyError):
        raise HRSyncError("HR 응답 형식을 해석할 수 없습니다(JSON 확인 필요).")
    return out


async def fetch_own_employee(user) -> dict | None:
    """**본인 HR 레코드만** 조회하는 자기검증 함수.

    보안 원칙(하드코딩): 개인 HR 정보는 오직 로그인한 '본인' 것만 제공한다.
    1) 조회 대상 사번은 **서버가 보유한 user.employee_no만** 사용(클라이언트 입력 사번 금지).
    2) 사번이 없으면 = 본인 확인 불가 → **무조건 거부**(None).
    3) HR 응답의 employee_no(및 이메일)를 허브가 **재검증** → 요청자 본인과 불일치하면,
       HR이 실수로 남의 데이터를 줬더라도 **거부**(None).
    확인되지 않으면 절대 반환하지 않는다.
    """
    empno = str(getattr(user, "employee_no", "") or "").strip()
    if not empno:
        return None                          # 본인 확인 불가 → 제공 거부
    base = (settings.hr_base_url or "").strip().rstrip("/")
    if not base:
        raise HRSyncError("HR 서버 주소가 설정되지 않았습니다 (.env HR_BASE_URL).")
    path = (settings.hr_employees_path or "/api/v1/employees").rstrip("/")
    url = f"{base}{path}/{quote(empno, safe='')}"
    try:
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
            r = await client.get(url, headers=_auth_headers())
            if r.status_code == 404:
                return None
            r.raise_for_status()
            rec = r.json()
    except httpx.HTTPStatusError as e:
        raise HRSyncError(f"HR 서버 응답 오류 (HTTP {e.response.status_code})")
    except httpx.HTTPError as e:
        raise HRSyncError(f"HR 서버에 연결할 수 없습니다 ({type(e).__name__})")
    except ValueError:
        raise HRSyncError("HR 응답 형식을 해석할 수 없습니다.")
    if isinstance(rec, dict) and isinstance(rec.get("employee"), dict):
        rec = rec["employee"]                # {"employee": {...}} 래핑 허용
    if not isinstance(rec, dict):
        return None
    # ── 방어층: 받은 데이터가 정말 '본인' 것인지 재검증 ──
    got_no = str(_g(rec, "employee_no", "emp_no", "empNo", "사번", default="")).strip()
    if not got_no or got_no != empno:
        return None                          # 사번 불일치 → 거부(남의 데이터 유출 차단)
    got_email = _g(rec, "email", "이메일")
    if got_email and getattr(user, "email", None):
        if str(got_email).strip().lower() != str(user.email).strip().lower():
            return None                      # 이메일까지 불일치 → 거부
    return rec


async def submit_document(user, doc_type: str, title: str, file_bytes: bytes,
                          filename: str, content_type: str = "application/pdf") -> dict:
    """기안: 생성 서류를 HR 결재로 전송한다(방향 ③).

    본인 확인: author_employee_no는 **로그인한 본인 사번만** 실어 보낸다(클라 입력 아님).
    사번이 없으면 본인 확인 불가로 전송을 거부한다.
    """
    import base64
    import hashlib
    empno = str(getattr(user, "employee_no", "") or "").strip()
    if not empno:
        raise HRSyncError("본인 사번이 없어 기안할 수 없습니다(HR 동기화로 사번 연결 후 이용).")
    base = (settings.hr_base_url or "").strip().rstrip("/")
    if not base:
        raise HRSyncError("HR 서버 주소가 설정되지 않았습니다 (.env HR_BASE_URL).")
    url = base + (settings.hr_documents_path or "/api/v1/documents/ingest")
    payload = {
        "doc_type": doc_type,
        "title": title,
        "author_employee_no": empno,          # 본인 확인 정보(본인 사번만)
        "author_name": getattr(user, "name", None) or getattr(user, "username", None),
        "file": {"filename": filename, "content_type": content_type,
                 "content_base64": base64.b64encode(file_bytes).decode("ascii")},
    }
    headers = _auth_headers()
    # 멱등키는 반드시 ASCII(HTTP 헤더 제약). 한글 파일명 등이 섞이지 않도록 해시로 생성.
    seed = f"{getattr(user, 'id', '')}-{filename}".encode("utf-8")
    headers["Idempotency-Key"] = "hub-doc-" + hashlib.md5(seed).hexdigest()[:20]
    try:
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
            r = await client.post(url, json=payload, headers=headers)
            r.raise_for_status()
    except httpx.HTTPStatusError as e:
        raise HRSyncError(f"HR 결재 접수 실패 (HTTP {e.response.status_code})")
    except httpx.HTTPError as e:
        raise HRSyncError(f"HR 서버에 연결할 수 없습니다 ({type(e).__name__})")
    try:
        data = r.json() if r.content else {}
    except ValueError:
        data = {}
    return {"hr_document_id": _g(data, "hr_document_id", "document_id", "id"),
            "approval_status": _g(data, "approval_status", "status", default="pending")}


def _norm_approval(s) -> str | None:
    """HR 결재 상태 문자열을 pending/approved/rejected로 정규화."""
    s = str(s or "").strip().lower()
    if s in ("approved", "approve", "승인", "완료", "confirmed", "done", "accept", "accepted"):
        return "approved"
    if s in ("rejected", "reject", "반려", "거절", "denied", "declined", "return", "returned"):
        return "rejected"
    if s in ("pending", "submitted", "in_progress", "progress", "결재중", "진행중", "waiting", "review"):
        return "pending"
    return None


async def fetch_document_status(hr_ref: str) -> dict | None:
    """기안한 서류의 HR 결재 상태를 조회(폴링). hr_document_status_path 미설정이면 None.
    반환: {"status": pending|approved|rejected, "note": ...} 또는 상태 해석불가 시 None."""
    ref = str(hr_ref or "").strip()
    tmpl = (settings.hr_document_status_path or "").strip()
    base = (settings.hr_base_url or "").strip().rstrip("/")
    if not ref or not tmpl or not base:
        return None
    path = tmpl.replace("{ref}", quote(ref, safe="")).replace("{id}", quote(ref, safe=""))
    if "{" not in tmpl and "}" not in tmpl:      # 치환자 없으면 경로 끝에 붙임
        path = tmpl.rstrip("/") + "/" + quote(ref, safe="")
    try:
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
            r = await client.get(base + path, headers=_auth_headers())
            if r.status_code == 404:
                return None
            r.raise_for_status()
            data = r.json() if r.content else {}
    except (httpx.HTTPError, ValueError):
        return None
    if isinstance(data, dict):
        for wrap in ("document", "data", "result"):
            if isinstance(data.get(wrap), dict):
                data = data[wrap]
                break
    status = _norm_approval(_g(data, "approval_status", "status", "state"))
    if not status:
        return None
    note = _g(data, "approval_note", "note", "comment", "reason", "코멘트", "사유")
    return {"status": status, "note": (str(note)[:300] if note else None)}


async def sync_employees(db: AsyncSession, updated_since: str | None = None,
                         auto_create: bool | None = None) -> dict:
    """HR 명부로 계정을 동기화하고 처리 결과 요약을 반환."""
    if auto_create is None:
        auto_create = bool(settings.hr_auto_create)
    emps = await fetch_employees(updated_since)

    try:
        role_map = json.loads(settings.hr_role_map or "{}")
    except (ValueError, TypeError):
        role_map = {}
    try:
        default_role = UserRole(settings.hr_default_role or "user")
    except ValueError:
        default_role = UserRole.user

    users = (await db.execute(select(User))).scalars().all()
    by_empno = {u.employee_no: u for u in users if u.employee_no}
    by_email = {u.email.lower(): u for u in users if u.email}

    created, updated, deactivated, skipped, errors = [], 0, 0, 0, []
    for e in emps:
        empno = str(_g(e, "employee_no", "emp_no", "empNo", "사번", default="") or "").strip()
        if not empno:
            errors.append({"name": _g(e, "name", default="?"), "error": "employee_no 없음"})
            continue
        name = _g(e, "name", "emp_name", "성명")
        email = _g(e, "email", "이메일")
        dept = _g(e, "department_name", "department", "dept_name", "부서")
        position = _g(e, "position", "job_grade", "직위", "직급")
        status = _norm_status(_g(e, "status", "재직상태"))
        u = by_empno.get(empno) or (by_email.get(str(email).lower()) if email else None)
        try:
            if status == "active":
                if u:
                    if u.role == UserRole.superadmin:
                        skipped += 1
                        continue
                    u.employee_no = empno
                    if name:
                        u.name = name
                    if dept:
                        u.department = dept
                    if position:
                        u.position = position
                    if not u.is_active:
                        u.is_active = True
                    updated += 1
                elif auto_create:
                    uname = str(email or empno)[:50]
                    if uname in {x.username for x in users} or uname in [c["username"] for c in created]:
                        errors.append({"employee_no": empno, "error": f"username 중복({uname})"})
                        continue
                    temp = secrets.token_urlsafe(9)
                    try:
                        role = UserRole(role_map.get(position or "", default_role.value))
                    except ValueError:
                        role = UserRole.user
                    if role == UserRole.superadmin:   # HR 데이터로 최고관리자 승격 금지
                        role = UserRole.admin
                    db.add(User(username=uname, email=(email or None), name=name, department=dept,
                                position=position, employee_no=empno, role=role, is_active=True,
                                force_password_reset=True, hashed_password=pwd_context.hash(temp)))
                    created.append({"username": uname, "employee_no": empno, "temp_password": temp})
                else:
                    skipped += 1
            else:  # leave / resigned → 로그인 차단(데이터 보존)
                if u and u.role != UserRole.superadmin and u.is_active:
                    u.is_active = False
                    u.token_version = (u.token_version or 0) + 1
                    deactivated += 1
        except Exception as ex:  # noqa: BLE001
            errors.append({"employee_no": empno, "error": str(ex)[:200]})

    await db.commit()
    return {"fetched": len(emps), "created": created, "updated": updated,
            "deactivated": deactivated, "skipped": skipped, "errors": errors}
