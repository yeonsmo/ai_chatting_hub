import threading
import time
from datetime import datetime, timedelta
from fastapi import APIRouter, Depends, HTTPException, Request
from jose import jwt
from passlib.context import CryptContext
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from app.config import settings
from app.database import get_db
from app.dependencies import get_current_user
from app.models import User, UsageLog
from app.schemas import LoginRequest, TokenResponse, PasswordChangeRequest

router = APIRouter(prefix="/auth", tags=["auth"])
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

# ---- 로그인 브루트포스 방지 (프로세스 내 슬라이딩 윈도우) ----
# 참고: 워커가 여러 개면 프로세스별로 카운트되므로, 강한 보호가 필요하면
# 리버스 프록시/공유 저장소(redis) 기반 리밋을 함께 두는 것을 권장.
_LOGIN_FAILS: dict[str, list[float]] = {}
_LOGIN_LOCK = threading.Lock()
_FAIL_MAX = 10           # (IP+계정) 조합 윈도우 내 최대 실패 횟수
_IP_FAIL_MAX = 30        # IP 전체(계정 무관) 실패 상한 — 패스워드 스프레잉 차단
_FAIL_WINDOW = 300.0     # 초
# 존재하지 않는 사용자에서도 bcrypt를 돌려 응답 시간을 평준화(계정 열거 방지)
_DUMMY_HASH = pwd_context.hash("timing-equalizer-not-a-real-password")


def _rl_key(ip: str, username: str) -> str:
    return f"{ip}|{username.lower()[:50]}"


def _ip_key(ip: str) -> str:
    return f"ip|{ip}"


def _fails_within(key: str, now: float) -> list[float]:
    arr = [t for t in _LOGIN_FAILS.get(key, []) if now - t < _FAIL_WINDOW]
    _LOGIN_FAILS[key] = arr
    return arr


def _is_rate_limited(ip: str, username: str) -> bool:
    """(IP+계정) 조합 한도 또는 IP 전체 한도 중 하나라도 초과하면 True."""
    now = time.monotonic()
    with _LOGIN_LOCK:
        combo = _fails_within(_rl_key(ip, username), now)
        ip_all = _fails_within(_ip_key(ip), now)
        return len(combo) >= _FAIL_MAX or len(ip_all) >= _IP_FAIL_MAX


def _record_login_fail(ip: str, username: str) -> None:
    now = time.monotonic()
    with _LOGIN_LOCK:
        _LOGIN_FAILS.setdefault(_rl_key(ip, username), []).append(now)
        _LOGIN_FAILS.setdefault(_ip_key(ip), []).append(now)
        if len(_LOGIN_FAILS) > 5000:  # 메모리 상한(오래된 항목 정리)
            for k in [k for k, v in _LOGIN_FAILS.items() if not v or now - v[-1] > _FAIL_WINDOW]:
                _LOGIN_FAILS.pop(k, None)


def _clear_login_fail(ip: str, username: str) -> None:
    # 로그인 성공 시 해당 계정 조합만 초기화(같은 IP의 다른 실패 누적은 유지)
    with _LOGIN_LOCK:
        _LOGIN_FAILS.pop(_rl_key(ip, username), None)


def create_access_token(user: User) -> str:
    expire = datetime.utcnow() + timedelta(minutes=settings.access_token_expire_minutes)
    payload = {"sub": user.username, "exp": expire, "ver": user.token_version or 0}
    return jwt.encode(payload, settings.secret_key, algorithm=settings.algorithm)


def client_ip(request: Request) -> str:
    return getattr(request.state, "client_ip", None) or (request.client.host if request.client else "")


@router.post("/login", response_model=TokenResponse)
async def login(request: LoginRequest, http_request: Request, db: AsyncSession = Depends(get_db)):
    ip = client_ip(http_request)
    if _is_rate_limited(ip, request.username):
        db.add(UsageLog(
            username=request.username[:50], action="login_failed",
            status="error", detail="로그인 시도 제한 초과", client_ip=ip,
        ))
        await db.commit()
        raise HTTPException(status_code=429, detail="로그인 시도가 너무 많습니다. 잠시 후 다시 시도하세요.")

    result = await db.execute(select(User).where(User.username == request.username))
    user = result.scalar_one_or_none()

    # 사용자 존재 여부와 무관하게 항상 bcrypt를 1회 수행 → 타이밍 기반 계정 열거 차단
    if user:
        valid = pwd_context.verify(request.password, user.hashed_password)
    else:
        pwd_context.verify(request.password, _DUMMY_HASH)
        valid = False

    if not valid:
        _record_login_fail(ip, request.username)
        db.add(UsageLog(
            user_id=user.id if user else None,
            username=request.username[:50],
            action="login_failed",
            status="error",
            detail="아이디 또는 비밀번호 불일치",
            client_ip=ip,
        ))
        await db.commit()
        raise HTTPException(status_code=401, detail="아이디 또는 비밀번호가 틀렸습니다")
    if not user.is_active:
        # 비활성 계정에 대한 유효 비밀번호 시도도 감사 로그로 남긴다
        db.add(UsageLog(
            user_id=user.id, username=user.username, action="login_failed",
            status="error", detail="비활성화된 계정 로그인 시도", client_ip=ip,
        ))
        await db.commit()
        raise HTTPException(status_code=403, detail="비활성화된 계정입니다")

    _clear_login_fail(ip, user.username)
    db.add(UsageLog(
        user_id=user.id, username=user.username, action="login",
        client_ip=ip,
    ))
    await db.commit()

    return TokenResponse(
        access_token=create_access_token(user),
        force_password_reset=user.force_password_reset,
    )


@router.post("/change-password")
async def change_password(
    request: PasswordChangeRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if not pwd_context.verify(request.current_password, current_user.hashed_password):
        raise HTTPException(status_code=400, detail="현재 비밀번호가 틀렸습니다")
    if len(request.new_password) < 8:
        raise HTTPException(status_code=400, detail="비밀번호는 8자 이상이어야 합니다")

    current_user.hashed_password = pwd_context.hash(request.new_password)
    current_user.force_password_reset = False
    # 기존 토큰 전부 무효화(탈취 대비) 후, 클라이언트가 이어 쓸 새 토큰 발급
    current_user.token_version = (current_user.token_version or 0) + 1
    await db.commit()
    await db.refresh(current_user)
    return {"message": "비밀번호가 변경되었습니다", "access_token": create_access_token(current_user)}


@router.get("/me")
async def get_me(current_user: User = Depends(get_current_user)):
    return {
        "id": current_user.id,
        "username": current_user.username,
        "email": current_user.email,
        "role": current_user.role,
        "force_password_reset": current_user.force_password_reset,
    }
