"""관리자 설정 라우터: 모델 라우팅 / 외부 연동 / 스킬 (관리자 이상)."""
import json
import re

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from app.database import get_db
from app.dependencies import get_admin_user
from app.models import User, ModelRoute, Integration, Skill
from app.providers import PROVIDERS
from app.roles import ROLE_LEVELS, ROLE_LABELS_KO
from app.skills_runtime import parse_params_schema
from app.schemas import (
    ModelRouteUpsert, ModelRouteResponse,
    IntegrationCreate, IntegrationUpdate, IntegrationResponse,
    SkillCreate, SkillUpdate, SkillResponse, SkillParam,
)

router = APIRouter(prefix="/settings", tags=["settings"])

SKILL_NAME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9_-]{1,63}$")


@router.get("/meta")
async def settings_meta(_: User = Depends(get_admin_user)):
    """프론트 설정 화면용 메타데이터: 프로바이더 목록, 역할 목록."""
    return {
        "providers": [
            {"key": k, "label": v["label"], "extra_fields": v["extra_fields"]}
            for k, v in PROVIDERS.items()
        ],
        "roles": [
            {"key": k, "label": ROLE_LABELS_KO.get(k, k), "level": v}
            for k, v in sorted(ROLE_LEVELS.items(), key=lambda x: x[1])
        ],
    }


# ================= 모델 라우팅 =================

def _validate_role(role: str):
    if role not in ROLE_LEVELS:
        raise HTTPException(status_code=400, detail=f"알 수 없는 역할: {role}")


@router.get("/model-routes", response_model=list[ModelRouteResponse])
async def list_model_routes(
    _: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(ModelRoute).order_by(ModelRoute.sort.asc(), ModelRoute.id.asc()))
    return result.scalars().all()


@router.post("/model-routes", response_model=ModelRouteResponse)
async def create_model_route(
    request: ModelRouteUpsert,
    _: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db),
):
    if request.provider not in PROVIDERS:
        raise HTTPException(status_code=400, detail=f"알 수 없는 프로바이더: {request.provider}")
    _validate_role(request.min_role)
    key = request.key.strip()
    if not key or not request.provider_model_id.strip():
        raise HTTPException(status_code=400, detail="모델 키와 프로바이더 모델 ID를 입력해주세요")
    exists = await db.scalar(select(ModelRoute.id).where(ModelRoute.key == key))
    if exists:
        raise HTTPException(status_code=400, detail="이미 존재하는 모델 키입니다")
    route = ModelRoute(
        key=key[:80], label=request.label.strip()[:120] or key,
        provider=request.provider, provider_model_id=request.provider_model_id.strip()[:200],
        description=request.description.strip()[:200],
        min_role=request.min_role, enabled=request.enabled, sort=request.sort,
    )
    db.add(route)
    await db.commit()
    await db.refresh(route)
    return route


@router.patch("/model-routes/{route_id}", response_model=ModelRouteResponse)
async def update_model_route(
    route_id: int,
    request: ModelRouteUpsert,
    _: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(ModelRoute).where(ModelRoute.id == route_id))
    route = result.scalar_one_or_none()
    if not route:
        raise HTTPException(status_code=404, detail="라우팅을 찾을 수 없습니다")
    if request.provider not in PROVIDERS:
        raise HTTPException(status_code=400, detail=f"알 수 없는 프로바이더: {request.provider}")
    _validate_role(request.min_role)

    route.label = request.label.strip()[:120] or route.label
    route.provider = request.provider
    route.provider_model_id = request.provider_model_id.strip()[:200] or route.provider_model_id
    route.description = request.description.strip()[:200]
    route.min_role = request.min_role
    route.enabled = request.enabled
    route.sort = request.sort
    await db.commit()
    await db.refresh(route)
    return route


@router.delete("/model-routes/{route_id}")
async def delete_model_route(
    route_id: int,
    _: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(ModelRoute).where(ModelRoute.id == route_id))
    route = result.scalar_one_or_none()
    if not route:
        raise HTTPException(status_code=404, detail="라우팅을 찾을 수 없습니다")
    await db.delete(route)
    await db.commit()
    return {"message": "삭제되었습니다"}


# ================= 연동 =================

async def _integration_response(i: Integration, db: AsyncSession) -> IntegrationResponse:
    count = await db.scalar(select(func.count(Skill.id)).where(Skill.integration_id == i.id))
    return IntegrationResponse(
        id=i.id, name=i.name, kind=i.kind, base_url=i.base_url,
        auth_type=i.auth_type, auth_key=i.auth_key,
        has_credential=bool(i.credential),
        is_active=i.is_active, skill_count=int(count or 0), created_at=i.created_at,
    )


@router.get("/integrations", response_model=list[IntegrationResponse])
async def list_integrations(
    _: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(Integration).order_by(Integration.id.asc()))
    return [await _integration_response(i, db) for i in result.scalars().all()]


@router.post("/integrations", response_model=IntegrationResponse)
async def create_integration(
    request: IntegrationCreate,
    current_user: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db),
):
    base = request.base_url.strip()
    if not base.startswith(("http://", "https://")):
        raise HTTPException(status_code=400, detail="base_url은 http(s)://로 시작해야 합니다")
    if request.auth_type not in ("bearer", "header", "query", "none"):
        raise HTTPException(status_code=400, detail="auth_type은 bearer/header/query/none 중 하나여야 합니다")
    integ = Integration(
        name=request.name.strip()[:100] or "연동",
        kind=request.kind.strip()[:50] or "custom",
        base_url=base[:500],
        auth_type=request.auth_type,
        auth_key=request.auth_key.strip()[:100] or "Authorization",
        credential=request.credential.strip()[:1000],
        extra_headers=json.dumps(request.extra_headers, ensure_ascii=False) if request.extra_headers else None,
        created_by=current_user.id,
    )
    db.add(integ)
    await db.commit()
    await db.refresh(integ)
    return await _integration_response(integ, db)


@router.patch("/integrations/{integ_id}", response_model=IntegrationResponse)
async def update_integration(
    integ_id: int,
    request: IntegrationUpdate,
    _: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(Integration).where(Integration.id == integ_id))
    integ = result.scalar_one_or_none()
    if not integ:
        raise HTTPException(status_code=404, detail="연동을 찾을 수 없습니다")

    if request.name is not None:
        integ.name = request.name.strip()[:100] or integ.name
    if request.kind is not None:
        integ.kind = request.kind.strip()[:50] or integ.kind
    if request.base_url is not None:
        base = request.base_url.strip()
        if not base.startswith(("http://", "https://")):
            raise HTTPException(status_code=400, detail="base_url은 http(s)://로 시작해야 합니다")
        integ.base_url = base[:500]
    if request.auth_type is not None:
        if request.auth_type not in ("bearer", "header", "query", "none"):
            raise HTTPException(status_code=400, detail="잘못된 auth_type")
        integ.auth_type = request.auth_type
    if request.auth_key is not None:
        integ.auth_key = request.auth_key.strip()[:100] or integ.auth_key
    if request.credential:  # 빈 문자열이면 기존 값 유지
        integ.credential = request.credential.strip()[:1000]
    if request.extra_headers is not None:
        integ.extra_headers = json.dumps(request.extra_headers, ensure_ascii=False) if request.extra_headers else None
    if request.is_active is not None:
        integ.is_active = request.is_active
    await db.commit()
    await db.refresh(integ)
    return await _integration_response(integ, db)


@router.delete("/integrations/{integ_id}")
async def delete_integration(
    integ_id: int,
    _: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(Integration).where(Integration.id == integ_id))
    integ = result.scalar_one_or_none()
    if not integ:
        raise HTTPException(status_code=404, detail="연동을 찾을 수 없습니다")
    await db.delete(integ)  # 소속 스킬은 cascade 삭제
    await db.commit()
    return {"message": "연동과 소속 스킬이 삭제되었습니다"}


# ================= 스킬 =================

def _skill_response(s: Skill, integration_name: str = "") -> SkillResponse:
    try:
        query = json.loads(s.query_template) if s.query_template else None
    except (ValueError, TypeError):
        query = None
    return SkillResponse(
        id=s.id, name=s.name, title=s.title, description=s.description,
        integration_id=s.integration_id, integration_name=integration_name,
        method=s.method, path=s.path,
        query_template=query, body_template=s.body_template,
        params=[SkillParam(**p) for p in parse_params_schema(s)],
        min_role=s.min_role, timeout_s=s.timeout_s, is_active=s.is_active,
        created_at=s.created_at,
    )


@router.get("/skills", response_model=list[SkillResponse])
async def list_skills(
    _: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Skill, Integration.name)
        .join(Integration, Skill.integration_id == Integration.id)
        .order_by(Skill.id.asc())
    )
    return [_skill_response(s, name) for s, name in result.all()]


async def _validate_skill_common(request, db: AsyncSession):
    if request.integration_id is not None:
        exists = await db.scalar(select(Integration.id).where(Integration.id == request.integration_id))
        if not exists:
            raise HTTPException(status_code=400, detail="연동을 찾을 수 없습니다")
    if getattr(request, "min_role", None) is not None:
        _validate_role(request.min_role)
    if getattr(request, "method", None) is not None and request.method.upper() not in ("GET", "POST", "PUT", "PATCH", "DELETE"):
        raise HTTPException(status_code=400, detail="method는 GET/POST/PUT/PATCH/DELETE 중 하나여야 합니다")


@router.post("/skills", response_model=SkillResponse)
async def create_skill(
    request: SkillCreate,
    current_user: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db),
):
    name = request.name.strip()
    if not SKILL_NAME_RE.match(name):
        raise HTTPException(status_code=400, detail="스킬 이름은 영문으로 시작하는 영문/숫자/_/- 2~64자여야 합니다")
    if not request.title.strip() or not request.description.strip():
        raise HTTPException(status_code=400, detail="표시 이름과 설명을 입력해주세요")
    await _validate_skill_common(request, db)
    exists = await db.scalar(select(Skill.id).where(Skill.name == name))
    if exists:
        raise HTTPException(status_code=400, detail="이미 존재하는 스킬 이름입니다")

    skill = Skill(
        name=name, title=request.title.strip()[:120], description=request.description.strip(),
        integration_id=request.integration_id,
        method=request.method.upper(), path=request.path.strip()[:500],
        query_template=json.dumps(request.query_template, ensure_ascii=False) if request.query_template else None,
        body_template=request.body_template,
        params_schema=json.dumps([p.model_dump() for p in request.params], ensure_ascii=False),
        min_role=request.min_role, timeout_s=max(3, min(request.timeout_s, 60)),
        is_active=request.is_active, created_by=current_user.id,
    )
    db.add(skill)
    await db.commit()
    await db.refresh(skill)
    integ_name = await db.scalar(select(Integration.name).where(Integration.id == skill.integration_id))
    return _skill_response(skill, integ_name or "")


@router.patch("/skills/{skill_id}", response_model=SkillResponse)
async def update_skill(
    skill_id: int,
    request: SkillUpdate,
    _: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(Skill).where(Skill.id == skill_id))
    skill = result.scalar_one_or_none()
    if not skill:
        raise HTTPException(status_code=404, detail="스킬을 찾을 수 없습니다")
    await _validate_skill_common(request, db)

    if request.title is not None:
        skill.title = request.title.strip()[:120] or skill.title
    if request.description is not None:
        skill.description = request.description.strip() or skill.description
    if request.integration_id is not None:
        skill.integration_id = request.integration_id
    if request.method is not None:
        skill.method = request.method.upper()
    if request.path is not None:
        skill.path = request.path.strip()[:500] or skill.path
    if request.query_template is not None:
        skill.query_template = json.dumps(request.query_template, ensure_ascii=False) if request.query_template else None
    if request.body_template is not None:
        skill.body_template = request.body_template or None
    if request.params is not None:
        skill.params_schema = json.dumps([p.model_dump() for p in request.params], ensure_ascii=False)
    if request.min_role is not None:
        skill.min_role = request.min_role
    if request.timeout_s is not None:
        skill.timeout_s = max(3, min(request.timeout_s, 60))
    if request.is_active is not None:
        skill.is_active = request.is_active
    await db.commit()
    await db.refresh(skill)
    integ_name = await db.scalar(select(Integration.name).where(Integration.id == skill.integration_id))
    return _skill_response(skill, integ_name or "")


@router.delete("/skills/{skill_id}")
async def delete_skill(
    skill_id: int,
    _: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(Skill).where(Skill.id == skill_id))
    skill = result.scalar_one_or_none()
    if not skill:
        raise HTTPException(status_code=404, detail="스킬을 찾을 수 없습니다")
    await db.delete(skill)
    await db.commit()
    return {"message": "삭제되었습니다"}
