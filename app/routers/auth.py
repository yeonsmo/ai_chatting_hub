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


def create_access_token(username: str) -> str:
    expire = datetime.utcnow() + timedelta(minutes=settings.access_token_expire_minutes)
    return jwt.encode({"sub": username, "exp": expire}, settings.secret_key, algorithm=settings.algorithm)


def client_ip(request: Request) -> str:
    return getattr(request.state, "client_ip", None) or (request.client.host if request.client else "")


@router.post("/login", response_model=TokenResponse)
async def login(request: LoginRequest, http_request: Request, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(User).where(User.username == request.username))
    user = result.scalar_one_or_none()

    if not user or not pwd_context.verify(request.password, user.hashed_password):
        db.add(UsageLog(
            user_id=user.id if user else None,
            username=request.username[:50],
            action="login_failed",
            status="error",
            detail="아이디 또는 비밀번호 불일치",
            client_ip=client_ip(http_request),
        ))
        await db.commit()
        raise HTTPException(status_code=401, detail="아이디 또는 비밀번호가 틀렸습니다")
    if not user.is_active:
        raise HTTPException(status_code=403, detail="비활성화된 계정입니다")

    db.add(UsageLog(
        user_id=user.id, username=user.username, action="login",
        client_ip=client_ip(http_request),
    ))
    await db.commit()

    return TokenResponse(
        access_token=create_access_token(user.username),
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
    await db.commit()
    return {"message": "비밀번호가 변경되었습니다"}


@router.get("/me")
async def get_me(current_user: User = Depends(get_current_user)):
    return {
        "id": current_user.id,
        "username": current_user.username,
        "email": current_user.email,
        "role": current_user.role,
        "force_password_reset": current_user.force_password_reset,
    }
