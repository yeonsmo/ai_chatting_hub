from fastapi import APIRouter, Depends, HTTPException
from passlib.context import CryptContext
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from app.database import get_db
from app.dependencies import get_admin_user, get_superadmin_user
from app.models import User, UserRole
from app.schemas import UserCreate, UserResponse

router = APIRouter(prefix="/users", tags=["users"])
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


@router.post("", response_model=UserResponse)
async def create_user(
    request: UserCreate,
    current_user: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db),
):
    if request.role == UserRole.superadmin:
        raise HTTPException(status_code=403, detail="최고 관리자 계정은 생성할 수 없습니다")
    if request.role == UserRole.admin and current_user.role != UserRole.superadmin:
        raise HTTPException(status_code=403, detail="관리자 계정은 최고 관리자만 생성할 수 있습니다")

    result = await db.execute(select(User).where(User.username == request.username))
    if result.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="이미 존재하는 사용자명입니다")

    user = User(
        username=request.username,
        email=request.email,
        hashed_password=pwd_context.hash(request.password),
        role=request.role,
        force_password_reset=True,
        created_by=current_user.id,
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


@router.get("", response_model=list[UserResponse])
async def list_users(
    current_user: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db),
):
    if current_user.role == UserRole.superadmin:
        result = await db.execute(select(User))
    else:
        result = await db.execute(select(User).where(User.created_by == current_user.id))
    return result.scalars().all()


@router.patch("/{user_id}/toggle-active")
async def toggle_user_active(
    user_id: int,
    current_user: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()

    if not user:
        raise HTTPException(status_code=404, detail="사용자를 찾을 수 없습니다")
    if user.role == UserRole.superadmin:
        raise HTTPException(status_code=403, detail="최고 관리자 계정은 변경할 수 없습니다")
    if current_user.role == UserRole.admin and user.created_by != current_user.id:
        raise HTTPException(status_code=403, detail="권한이 없습니다")

    user.is_active = not user.is_active
    await db.commit()
    return {"message": f"{'활성화' if user.is_active else '비활성화'} 완료", "is_active": user.is_active}


@router.delete("/{user_id}")
async def delete_user(
    user_id: int,
    current_user: User = Depends(get_superadmin_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()

    if not user or user.role == UserRole.superadmin:
        raise HTTPException(status_code=404, detail="사용자를 찾을 수 없습니다")

    await db.delete(user)
    await db.commit()
    return {"message": "사용자가 삭제되었습니다"}
