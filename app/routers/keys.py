import json
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


def to_response(key: APIKey) -> APIKeyResponse:
    """세션에 붙은 ORM 객체를 건드리지 않고 마스킹된 응답을 만든다."""
    try:
        extra_keys = sorted((json.loads(key.extra) or {}).keys()) if key.extra else []
    except (ValueError, TypeError):
        extra_keys = []
    return APIKeyResponse(
        id=key.id,
        name=key.name,
        provider=key.provider,
        key_value=mask_key(key.key_value),
        extra_keys=extra_keys,
        is_active=key.is_active,
        created_at=key.created_at,
    )


@router.post("", response_model=APIKeyResponse)
async def create_api_key(
    request: APIKeyCreate,
    current_user: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db),
):
    key_value = request.key_value.strip()
    if not key_value:
        raise HTTPException(status_code=400, detail="API 키 값을 입력해주세요")
    extra = {k: str(v).strip() for k, v in (request.extra or {}).items() if str(v).strip()}
    api_key = APIKey(
        name=request.name.strip()[:100],
        provider=request.provider.strip()[:50],
        key_value=key_value,
        extra=json.dumps(extra, ensure_ascii=False) if extra else None,
        created_by=current_user.id,
    )
    db.add(api_key)
    await db.commit()
    await db.refresh(api_key)
    return to_response(api_key)


@router.get("", response_model=list[APIKeyResponse])
async def list_api_keys(
    current_user: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(APIKey).order_by(APIKey.id.desc()))
    return [to_response(k) for k in result.scalars().all()]


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
