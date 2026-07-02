from fastapi import Depends, HTTPException, status, Request
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from jose import JWTError, jwt
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from app.config import settings
from app.database import get_db
from app.models import User, UserRole

security = HTTPBearer()

# 비밀번호 강제 변경 중인 계정이 예외적으로 접근 가능한 경로(접미사 매칭)
_RESET_ALLOWED_SUFFIXES = ("/auth/change-password", "/auth/me", "/auth/logout")


async def get_current_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials = Depends(security),
    db: AsyncSession = Depends(get_db),
) -> User:
    exc = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="인증 정보가 유효하지 않습니다",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = jwt.decode(credentials.credentials, settings.secret_key, algorithms=[settings.algorithm])
        username = payload.get("sub")
        if not username:
            raise exc
    except JWTError:
        raise exc

    result = await db.execute(select(User).where(User.username == username))
    user = result.scalar_one_or_none()

    if not user or not user.is_active:
        raise exc
    # 비밀번호 변경 후 발급 전 토큰 무효화 (누락 클레임은 0으로 간주 → 기존 토큰 호환)
    if payload.get("ver", 0) != (user.token_version or 0):
        raise exc

    # 비밀번호 강제 변경이 필요한 계정은 변경 관련 경로 외 접근 차단(서버측 강제)
    if user.force_password_reset and not request.url.path.rstrip("/").endswith(_RESET_ALLOWED_SUFFIXES):
        raise HTTPException(status_code=403, detail="비밀번호를 먼저 변경해야 합니다")
    return user


async def get_admin_user(current_user: User = Depends(get_current_user)) -> User:
    from app.roles import has_min_role
    if not has_min_role(current_user, UserRole.admin):
        raise HTTPException(status_code=403, detail="관리자 권한이 필요합니다")
    return current_user


async def get_manager_user(current_user: User = Depends(get_current_user)) -> User:
    from app.roles import has_min_role
    if not has_min_role(current_user, UserRole.manager):
        raise HTTPException(status_code=403, detail="담당자 이상의 권한이 필요합니다")
    return current_user


async def get_superadmin_user(current_user: User = Depends(get_current_user)) -> User:
    from app.roles import has_min_role
    if not has_min_role(current_user, UserRole.superadmin):
        raise HTTPException(status_code=403, detail="최고 관리자 권한이 필요합니다")
    return current_user
