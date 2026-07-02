from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from app.database import get_db
from app.dependencies import get_admin_user
from app.models import APIKey, User
from app.schemas import APIKeyCreate, APIKeyResponse

router = APIRouter(prefix="/keys", tags=["api-keys"])


def mask_key(key: str) -> str:
    if len(key) <= 8:
        return "****"
    return key[:4] + "****" + key[-4:]


@router.post("", response_model=APIKeyResponse)
async def create_api_key(
    request: APIKeyCreate,
    current_user: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db),
):
    api_key = APIKey(
        name=request.name,
        provider=request.provider,
        key_value=request.key_value,
        created_by=current_user.id,
    )
    db.add(api_key)
    await db.commit()
    await db.refresh(api_key)
    api_key.key_value = mask_key(api_key.key_value)
    return api_key


@router.get("", response_model=list[APIKeyResponse])
async def list_api_keys(
    current_user: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(APIKey))
    keys = result.scalars().all()
    for key in keys:
        key.key_value = mask_key(key.key_value)
    return keys


@router.patch("/{key_id}/toggle")
async def toggle_api_key(
    key_id: int,
    current_user: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(APIKey).where(APIKey.id == key_id))
    key = result.scalar_one_or_none()
    if not key:
        raise HTTPException(status_code=404, detail="API 키를 찾을 수 없습니다")
    key.is_active = not key.is_active
    await db.commit()
    return {"message": f"{'활성화' if key.is_active else '비활성화'} 완료", "is_active": key.is_active}


@router.delete("/{key_id}")
async def delete_api_key(
    key_id: int,
    current_user: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(APIKey).where(APIKey.id == key_id))
    key = result.scalar_one_or_none()
    if not key:
        raise HTTPException(status_code=404, detail="API 키를 찾을 수 없습니다")
    await db.delete(key)
    await db.commit()
    return {"message": "API 키가 삭제되었습니다"}
