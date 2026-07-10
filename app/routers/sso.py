"""SSO 로그인(OAuth2 Authorization Code) — 하이웍스 등 IdP에 본인인증 위임.

흐름:
  1) GET /auth/sso/login  → state(CSRF) 쿠키 설정 후 IdP 인가 URL로 리다이렉트
  2) IdP 로그인 → GET /auth/sso/callback?code&state
  3) state 검증 → code로 토큰 교환 → 사용자정보 조회(email/사번/이름)
  4) 허브 계정 매칭(사번 우선, 없으면 이메일) → 활성 계정이면 허브 JWT 발급
  5) 프론트로 리다이렉트(#sso=<jwt>). 미등록/비활성이면 오류로 리다이렉트.

보안:
- IdP 액세스토큰은 서버에서만 사용, 클라이언트에 노출하지 않음(허브 JWT만 전달).
- state 쿠키(httponly, samesite=lax)와 콜백 state 파라미터 일치 필수(CSRF 방지).
- 자동생성은 옵션. 기본은 사전 등록/HR 동기화된 계정만 로그인 허용.
"""
import secrets

import httpx
from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from passlib.context import CryptContext
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.models import User, UserRole, UsageLog
from app.routers.auth import create_access_token

router = APIRouter(prefix="/auth/sso", tags=["sso"])
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
_STATE_COOKIE = "hub_sso_state"


def sso_enabled() -> bool:
    return bool(settings.sso_enabled and settings.sso_authorize_url and settings.sso_token_url
                and settings.sso_userinfo_url and settings.sso_client_id
                and settings.sso_redirect_uri)


def _g(d, *keys, default=None):
    for k in keys:
        if isinstance(d, dict) and d.get(k) not in (None, ""):
            return d[k]
    return default


def _err_redirect(msg: str) -> RedirectResponse:
    from urllib.parse import quote
    return RedirectResponse(url=f"/#sso_error={quote(msg)}", status_code=303)


@router.get("/enabled")
async def sso_status():
    """로그인 화면에서 SSO 버튼 표시 여부(비인증 공개)."""
    return {"enabled": sso_enabled(), "label": settings.sso_label or "SSO"}


@router.get("/login")
async def sso_login():
    """IdP 인가 페이지로 리다이렉트."""
    if not sso_enabled():
        return _err_redirect("SSO가 설정되지 않았습니다")
    from urllib.parse import urlencode
    state = secrets.token_urlsafe(24)
    params = {"response_type": "code", "client_id": settings.sso_client_id,
              "redirect_uri": settings.sso_redirect_uri, "state": state}
    if settings.sso_scope:
        params["scope"] = settings.sso_scope
    url = settings.sso_authorize_url + ("&" if "?" in settings.sso_authorize_url else "?") + urlencode(params)
    resp = RedirectResponse(url=url, status_code=303)
    # state 쿠키(콜백에서 대조). 리다이렉트 왕복을 견디도록 SameSite=Lax.
    resp.set_cookie(_STATE_COOKIE, state, max_age=600, httponly=True, samesite="lax", path="/api/auth/sso")
    return resp


@router.get("/callback")
async def sso_callback(request: Request, db: AsyncSession = Depends(get_db),
                       code: str | None = None, state: str | None = None):
    if not sso_enabled():
        return _err_redirect("SSO가 설정되지 않았습니다")
    # CSRF: 쿠키 state와 콜백 state 일치 필수
    cookie_state = request.cookies.get(_STATE_COOKIE)
    if not code or not state or not cookie_state or not secrets.compare_digest(state, cookie_state):
        return _err_redirect("로그인 검증에 실패했습니다(state 불일치)")

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            tok = await client.post(settings.sso_token_url, data={
                "grant_type": "authorization_code", "code": code,
                "redirect_uri": settings.sso_redirect_uri,
                "client_id": settings.sso_client_id,
                "client_secret": settings.sso_client_secret,
            }, headers={"Accept": "application/json"})
            tok.raise_for_status()
            access_token = _g(tok.json(), "access_token")
            if not access_token:
                return _err_redirect("토큰 발급에 실패했습니다")
            ui = await client.get(settings.sso_userinfo_url,
                                  headers={"Authorization": f"Bearer {access_token}",
                                           "Accept": "application/json"})
            ui.raise_for_status()
            info = ui.json()
    except httpx.HTTPError:
        return _err_redirect("IdP 통신에 실패했습니다")
    except ValueError:
        return _err_redirect("IdP 응답을 해석할 수 없습니다")

    # 응답 래핑({data:{...}}/{user:{...}}) 관대 처리
    if isinstance(info, dict):
        for wrap in ("data", "user", "result", "userInfo"):
            if isinstance(info.get(wrap), dict):
                info = info[wrap]
                break
    email = _g(info, settings.sso_userinfo_email_field, "email", "mail")
    empno = _g(info, settings.sso_userinfo_empno_field, "employee_no", "emp_no", "사번")
    name = _g(info, settings.sso_userinfo_name_field, "name", "성명")

    # 계정 매칭: 사번 우선 → 이메일
    user = None
    if empno:
        user = (await db.execute(select(User).where(User.employee_no == str(empno)))).scalar_one_or_none()
    if not user and email:
        user = (await db.execute(select(User).where(User.email == str(email)))).scalar_one_or_none()

    if user is None:
        if not settings.sso_auto_create or not (email or empno):
            return _err_redirect("등록되지 않은 계정입니다. 관리자에게 문의하세요")
        try:
            role = UserRole(settings.sso_default_role or "user")
        except ValueError:
            role = UserRole.user
        if role == UserRole.superadmin:
            role = UserRole.admin
        user = User(username=str(email or empno)[:50], email=(str(email) if email else None),
                    name=(str(name) if name else None), employee_no=(str(empno) if empno else None),
                    role=role, is_active=True, force_password_reset=False,
                    hashed_password=pwd_context.hash(secrets.token_urlsafe(24)))
        db.add(user)
        await db.flush()
    elif not user.is_active:
        return _err_redirect("비활성화된 계정입니다. 관리자에게 문의하세요")
    else:
        # SSO 로그인 시 사번/이름 최신화(있으면)
        if empno and not user.employee_no:
            user.employee_no = str(empno)
        if name and not user.name:
            user.name = str(name)

    jwt = create_access_token(user)
    ip = getattr(request.state, "client_ip", None) or (request.client.host if request.client else "")
    db.add(UsageLog(user_id=user.id, username=user.username, action="login",
                    provider="sso", client_ip=ip))
    await db.commit()
    # 토큰은 URL 프래그먼트(#)로 전달 → 서버로그/Referer에 남지 않음. 프론트가 읽어 저장.
    resp = RedirectResponse(url=f"/#sso={jwt}", status_code=303)
    resp.delete_cookie(_STATE_COOKIE, path="/api/auth/sso")
    return resp
