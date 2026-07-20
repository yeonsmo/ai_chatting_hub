"""관리자 설정 라우터: 모델 라우팅 / 외부 연동 / 스킬 (관리자 이상)."""
import asyncio
import json
import re
import time

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from app.database import get_db
from app.dependencies import get_admin_user
from app.models import User, ModelRoute, Integration, Skill, UsageLog
from app.providers import (
    PROVIDERS, IMAGE_PROVIDERS, OPENAI_FAMILY, run_chat, resolve_credentials,
    friendly_api_error, list_provider_models, probe_openai_model,
)
from app.roles import ROLE_LEVELS, ROLE_LABELS_KO
from app.skills_runtime import parse_params_schema, _validate_public_host
from app.schemas import (
    ModelRouteUpsert, ModelRouteResponse,
    IntegrationCreate, IntegrationUpdate, IntegrationResponse,
    SkillCreate, SkillUpdate, SkillResponse, SkillParam,
)

router = APIRouter(prefix="/settings", tags=["settings"])

SKILL_NAME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9_-]{1,63}$")


def _validate_base_url(base_url: str) -> str:
    from urllib.parse import urlparse
    base = base_url.strip()
    if not base.startswith(("http://", "https://")):
        raise HTTPException(status_code=400, detail="base_url은 http(s)://로 시작해야 합니다")
    host = urlparse(base).hostname or ""
    if not host:
        raise HTTPException(status_code=400, detail="base_url에 호스트가 없습니다 (예: https://api.example.com)")
    try:
        _validate_public_host(host)  # 사설/loopback/링크로컬이면 예외
    except ValueError as e:
        # 해석 실패(내부 split-DNS 등)는 허용 — 실제 호출 시 런타임이 재검증한다.
        # 단, 사설/내부 대역으로 '해석되는' 경우는 등록 거부.
        if "내부" in str(e) or "사설" in str(e):
            raise HTTPException(status_code=400, detail="사설/내부 대역 주소는 연동 대상으로 등록할 수 없습니다")
    return base[:500]


def _validate_body_template(body_template):
    if body_template is None:
        return
    try:
        json.loads(body_template)
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail="body_template은 유효한 JSON이어야 합니다")


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
        kind="image" if request.kind == "image" else "chat",
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
    route.kind = "image" if request.kind == "image" else "chat"
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


class RouteTestBody(BaseModel):
    # 저장 전 값으로 바로 테스트할 수 있게 프로바이더/모델ID를 덮어쓸 수 있음(비우면 저장된 값)
    provider: str | None = None
    provider_model_id: str | None = None
    kind: str | None = None


# 테스트 남용(유료 호출 반복) 방지용 사용자별 쿨다운. 프로세스 로컬 소프트 가드.
_TEST_COOLDOWN_S = 2.0
_last_route_test: dict[int, float] = {}


@router.post("/model-routes/{route_id}/test")
async def test_model_route(
    route_id: int,
    body: RouteTestBody,
    current_user: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db),
):
    """이 모델 라우팅이 실제로 호출되는지 작은 요청으로 확인. 성공/실패와 프로바이더 원문
    오류를 그대로 돌려줘서, 키·모델ID·권한 문제를 관리자 화면에서 바로 진단하게 한다.
    유료 호출이므로 짧은 쿨다운을 두고, 결과를 감사 로그(action=test)에 남긴다."""
    route = (await db.execute(select(ModelRoute).where(ModelRoute.id == route_id))).scalar_one_or_none()
    if not route:
        raise HTTPException(status_code=404, detail="라우팅을 찾을 수 없습니다")
    provider = (body.provider or route.provider or "").strip()
    model_id = (body.provider_model_id or route.provider_model_id or "").strip()
    kind = (body.kind or route.kind or "chat").strip()
    if not provider or not model_id:
        return {"ok": False, "message": "프로바이더와 모델 ID를 먼저 입력하세요."}

    now = time.monotonic()
    last = _last_route_test.get(current_user.id, 0.0)
    if now - last < _TEST_COOLDOWN_S:
        return {"ok": False, "message": "너무 자주 테스트했습니다. 잠시 후 다시 시도하세요."}
    _last_route_test[current_user.id] = now

    started = now
    ok = False
    in_tok = out_tok = None
    try:
        if kind == "image":
            # 실제 이미지 생성은 매 클릭 과금되므로, 여기선 지원 여부와 키만 확인(생성 생략).
            if provider not in IMAGE_PROVIDERS:
                ok, note = False, "이 프로바이더는 이미지 생성을 지원하지 않습니다"
            else:
                await resolve_credentials(db, provider)   # 키 없으면 HTTPException
                ok, note = True, "이미지 지원 · 키 확인됨 (실제 생성은 비용 발생으로 생략)"
        else:
            messages = [{"role": "user", "content": "연결 테스트입니다. '연결됨'이라고만 답하세요."}]
            outcome = await run_chat(db, provider, model_id, None, messages, None)
            ok = True
            in_tok, out_tok = outcome.input_tokens, outcome.output_tokens
            reply = (getattr(outcome, "text", "") or "").strip().replace("\n", " ")
            note = f"응답: {reply[:40]}" if reply else "응답 수신"
        ms = int((time.monotonic() - started) * 1000)
        result = {"ok": ok, "message": f"연결 {'성공' if ok else '확인'} · {note} ({ms}ms)",
                  "provider": provider, "model_id": model_id}
        detail = note
        status = "success" if ok else "error"
    except HTTPException as e:
        result = {"ok": False, "message": str(e.detail), "provider": provider, "model_id": model_id}
        detail, status = str(e.detail), "error"
    except Exception as e:  # noqa: BLE001 — 프로바이더 원문 오류를 그대로 노출(진단용)
        result = {"ok": False, "message": friendly_api_error(e), "provider": provider, "model_id": model_id}
        detail, status = friendly_api_error(e), "error"

    # 유료 호출이므로 감사 로그에 남긴다(사용량/비용 추적 가능하게).
    db.add(UsageLog(user_id=current_user.id, username=current_user.username, action="test",
                    model=model_id, provider=provider, input_tokens=in_tok, output_tokens=out_tok,
                    status=status, detail=(detail or "")[:500],
                    duration_ms=int((time.monotonic() - started) * 1000)))
    await db.commit()
    return result


# ---------------- 프로바이더 모델 자동 검증·등록(가비아 등 OpenAI 호환) ----------------

class DiscoverBody(BaseModel):
    provider: str = "gabia"     # OpenAI 호환 프로바이더만(gabia/openai/azure)
    probe: bool = True          # 각 모델을 최소요청으로 실사용 검증(권장)
    auto_register: bool = True  # 검증 통과분을 라우팅에 자동 등록
    max_probe: int = 80         # 과금 폭주 방지 상한(검증 대상 개수)
    min_role: str = "user"      # 자동 등록 시 최소 권한


_DISCOVER_COOLDOWN_S = 15.0
_last_discover: dict[int, float] = {}
_PROBE_CONCURRENCY = 4
# 대화 모델이 아닌 게 명백한 접미/키워드(임베딩·음성·이미지·리랭커 등)는 검증 없이 건너뛴다.
_NON_CHAT_HINTS = ("embedding", "embed", "rerank", "reranker", "whisper", "tts", "audio",
                   "moderation", "image", "vision-ocr", "dall-e", "stable-diffusion",
                   "flux", "clip", "guard", "bge", "gte")


def _looks_non_chat(model_id: str) -> bool:
    m = model_id.lower()
    return any(h in m for h in _NON_CHAT_HINTS)


def _slug_key(model_id: str, taken: set[str]) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", model_id.lower()).strip("-")[:70] or "model"
    key, i = base, 2
    while key in taken:
        key = f"{base[:66]}-{i}"
        i += 1
    taken.add(key)
    return key


@router.post("/model-routes/discover")
async def discover_provider_models(
    body: DiscoverBody,
    current_user: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db),
):
    """가비아(OpenAI 호환) /v1/models를 호출해 '실제 노출되는 모델 전체'를 가져오고,
    각 모델을 최소 요청으로 검증한 뒤 통과한 대화 모델을 라우팅에 자동 등록한다.
    - 목록 조회 1회 + 검증 대상 수만큼의 최소 호출(상한 max_probe). 유료 호출이라 쿨다운.
    - 임베딩/음성/이미지 등 비대화 모델은 접미 힌트로 사전 제외하고, 검증에서 실패해도 제외.
    - 이미 등록된 모델ID는 건너뛴다(중복 방지)."""
    provider = (body.provider or "gabia").strip()
    if provider not in OPENAI_FAMILY:
        raise HTTPException(status_code=400,
                            detail="자동조회는 OpenAI 호환 프로바이더(가비아/OpenAI/Azure)만 지원합니다.")
    _validate_role(body.min_role)

    now = time.monotonic()
    last = _last_discover.get(current_user.id, 0.0)
    if now - last < _DISCOVER_COOLDOWN_S:
        raise HTTPException(status_code=429, detail="잠시 후 다시 시도하세요(자동조회 쿨다운).")
    _last_discover[current_user.id] = now

    # 자격증명 1회 확인(키 없으면 여기서 명확히 실패) + 전체 모델 목록 1회 조회
    key, extra = await resolve_credentials(db, provider)
    try:
        all_ids = await list_provider_models(db, provider)
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"모델 목록 조회 실패: {friendly_api_error(e)}")

    # 기존 라우팅(중복 방지용): 이 프로바이더에 이미 등록된 모델ID / 전체 키 집합
    rows = (await db.execute(select(ModelRoute))).scalars().all()
    existing_ids = {r.provider_model_id for r in rows if r.provider == provider}
    taken_keys = {r.key for r in rows}
    max_sort = max([r.sort for r in rows], default=0)

    # 검증 후보: 이미 등록된 것/비대화 힌트 제외
    candidates = [m for m in all_ids if m not in existing_ids and not _looks_non_chat(m)]
    skipped_noncat = [m for m in all_ids if _looks_non_chat(m) and m not in existing_ids]
    over_limit = candidates[body.max_probe:] if len(candidates) > body.max_probe else []
    candidates = candidates[:body.max_probe]

    working: list[tuple[str, str]] = []   # (model_id, note)
    failed: list[dict] = []

    if body.probe:
        sem = asyncio.Semaphore(_PROBE_CONCURRENCY)

        async def _probe(mid):
            async with sem:
                ok, note = await probe_openai_model(provider, key, extra, mid)
                return mid, ok, note

        for mid, ok, note in await asyncio.gather(*[_probe(m) for m in candidates]):
            (working if ok else failed).append((mid, note) if ok else {"id": mid, "note": note})
    else:
        # 검증 생략 모드: 후보 전체를 '등록 대상'으로 간주(사용자가 나중에 개별 테스트)
        working = [(m, "검증 생략") for m in candidates]

    # 검증 통과분 자동 등록
    added = []
    if body.auto_register and working:
        for mid, note in sorted(working):
            max_sort += 1
            k = _slug_key(mid, taken_keys)
            db.add(ModelRoute(
                key=k[:80], label=mid[:120], provider=provider,
                provider_model_id=mid[:200], kind="chat",
                description="자동 등록(가비아 검증됨)"[:200],
                min_role=body.min_role, enabled=True, sort=max_sort,
            ))
            added.append({"key": k, "model_id": mid, "note": note})
        # 감사 로그(요약 1건)
        db.add(UsageLog(user_id=current_user.id, username=current_user.username, action="discover",
                        model=f"{len(added)}개 등록", provider=provider, status="success",
                        detail=(f"목록 {len(all_ids)} · 검증 {len(candidates)} · "
                                f"성공 {len(working)} · 등록 {len(added)}")[:500]))
        await db.commit()

    return {
        "ok": True,
        "provider": provider,
        "total_listed": len(all_ids),
        "probed": len(candidates),
        "working": len(working),
        "added": added,
        "already_registered": sorted(existing_ids & set(all_ids)),
        "failed": failed,
        "skipped_non_chat": sorted(skipped_noncat),
        "skipped_over_limit": over_limit,
        "message": (f"목록 {len(all_ids)}개 중 {len(candidates)}개 검증 → "
                    f"{len(working)}개 사용 가능, {len(added)}개 신규 등록."),
    }


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
    base = _validate_base_url(request.base_url)
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
        integ.base_url = _validate_base_url(request.base_url)
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
    _validate_body_template(request.body_template)
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
    _validate_body_template(request.body_template)
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
