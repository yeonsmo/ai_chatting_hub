"""역할/권한 중앙 정의.

새 역할을 추가하려면:
1. models.UserRole에 enum 값 추가
2. 아래 ROLE_LEVELS에 레벨 부여 (숫자가 클수록 상위 권한)
3. (프론트) index.html의 ROLE_LABELS에 한글 이름 추가

기능별 최소 요구 레벨은 레벨 비교로 판정하므로,
중간 역할을 끼워 넣어도 기존 검사가 그대로 동작한다.
"""
from fastapi import Depends, HTTPException
from app.models import User, UserRole

ROLE_LEVELS: dict[str, int] = {
    UserRole.user.value: 0,
    UserRole.manager.value: 10,
    UserRole.admin.value: 20,
    UserRole.superadmin.value: 30,
}

ROLE_LABELS_KO: dict[str, str] = {
    UserRole.user.value: "일반 사용자",
    UserRole.manager.value: "담당자",
    UserRole.admin.value: "관리자",
    UserRole.superadmin.value: "최고 관리자",
}


def role_level(role) -> int:
    value = role.value if isinstance(role, UserRole) else str(role)
    return ROLE_LEVELS.get(value, 0)


def has_min_role(user: User, min_role) -> bool:
    return role_level(user.role) >= role_level(min_role)


def require_min_role(min_role: UserRole):
    """FastAPI 의존성 팩토리: 최소 역할 레벨 요구."""
    from app.dependencies import get_current_user

    async def checker(current_user: User = Depends(get_current_user)) -> User:
        if not has_min_role(current_user, min_role):
            raise HTTPException(
                status_code=403,
                detail=f"{ROLE_LABELS_KO.get(min_role.value, min_role.value)} 이상의 권한이 필요합니다",
            )
        return current_user

    return checker
