import base64
import json
import time
from datetime import datetime
from types import SimpleNamespace
from urllib.parse import quote
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import PlainTextResponse, StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from app.config import settings
from app.database import get_db
from app.dependencies import get_current_user
from app.file_utils import stored_path, write_bytes
from app.models import (User, Conversation, Message, Attachment, Project, UsageLog,
                        ModelRoute, Skill, Integration)
from app.providers import (run_chat, run_chat_stream, run_image, friendly_api_error, ToolContext,
                           ANTHROPIC_FAMILY, PROVIDERS)
from app.roles import role_level, has_min_role
from app.routers.files import to_response as attachment_response
from app.skills_runtime import execute_skill, anthropic_tool_def, openai_tool_def
from app import gen_tools, knowledge
from app.routers import reference
from app.schemas import MessageCreate, ChatResponse, ConversationResponse, ConversationUpdate, MessageResponse

router = APIRouter(prefix="/chat", tags=["chat"])

_SEARCH_DOCS_DESC = (
    "사내 자료실(회사 기술·규격 자료: 배관 규격/치수/중량, 플랜지·볼트 규격, 재질·인장강도 등)에서 "
    "질문과 관련된 표/구획을 검색한다. 배관·규격·수치 관련 질문이면 추측하지 말고 이 도구로 자료를 "
    "찾아 그 값을 근거로 답하라. query에는 핵심 키워드를 넣는다(예: '8인치 sch40 무게', "
    "'ANSI 300 플랜지 볼트 수', '스테인리스 인장강도')."
)
_SEARCH_DOCS_SCHEMA = {"type": "object",
                       "properties": {"query": {"type": "string", "description": "검색 키워드/질문"}},
                       "required": ["query"]}
SEARCH_COMPANY_DOCS_ANTHROPIC = {"name": "search_company_docs", "description": _SEARCH_DOCS_DESC,
                                 "input_schema": _SEARCH_DOCS_SCHEMA}
SEARCH_COMPANY_DOCS_OPENAI = {"type": "function", "function": {
    "name": "search_company_docs", "description": _SEARCH_DOCS_DESC, "parameters": _SEARCH_DOCS_SCHEMA}}

CLAUDE_IMAGE_TYPES = {"image/jpeg", "image/png", "image/gif", "image/webp"}
MAX_IMAGE_BYTES = 5 * 1024 * 1024     # Claude 이미지 블록 제한
MAX_PDF_BYTES = 10 * 1024 * 1024      # PDF 원본 전송 상한
HISTORY_ATTACH_CHARS = 4_000          # 과거 턴 첨부 발췌 길이
CURRENT_ATTACH_CHARS = 60_000         # 현재 턴 첨부 텍스트 길이
PROJECT_FILE_CHARS = 30_000           # 프로젝트 지식 파일당 길이
PROJECT_TOTAL_CHARS = 120_000         # 프로젝트 지식 전체 상한


async def get_model_route(db: AsyncSession, key: str, user: User) -> ModelRoute:
    """모델 키 → 라우팅 조회 + 사용 가능 여부(활성/권한) 검사."""
    result = await db.execute(select(ModelRoute).where(ModelRoute.key == key))
    route = result.scalar_one_or_none()
    if not route or not route.enabled:
        raise HTTPException(status_code=404, detail=f"사용할 수 없는 모델입니다: {key}")
    if role_level(user.role) < role_level(route.min_role):
        raise HTTPException(status_code=403, detail="이 모델을 사용할 권한이 없습니다")
    return route


async def get_allowed_skills(db: AsyncSession, user: User) -> list[Skill]:
    """역할 레벨에 따라 사용 가능한 활성 스킬 목록 (integration eager 로딩)."""
    from sqlalchemy.orm import selectinload
    result = await db.execute(
        select(Skill)
        .join(Integration, Skill.integration_id == Integration.id)
        .where(Skill.is_active == True, Integration.is_active == True)
        .options(selectinload(Skill.integration))
        .order_by(Skill.id.asc())
    )
    return [s for s in result.scalars().all() if role_level(user.role) >= role_level(s.min_role)]


def _read_file_bytes(att: Attachment) -> bytes | None:
    try:
        with open(stored_path(att.stored_name), "rb") as f:
            return f.read()
    except OSError:
        return None


def _attachment_note(att: Attachment, limit: int) -> str:
    """텍스트 추출본이 있으면 발췌, 없으면 파일 정보만."""
    if att.text_content:
        body = att.text_content[:limit]
        truncated = " (일부 발췌)" if len(att.text_content) > limit else ""
        return f"\n\n[첨부파일: {att.filename}{truncated}]\n{body}\n[첨부파일 끝]"
    return f"\n\n[첨부파일: {att.filename} ({att.content_type}, {att.size_bytes:,} bytes) — 내용을 텍스트로 읽을 수 없는 형식입니다]"


def build_history_text(msg: Message, attachments: list[Attachment]) -> str:
    text = msg.content
    for att in attachments:
        text += _attachment_note(att, HISTORY_ATTACH_CHARS)
    return text


def build_claude_current_content(content: str, attachments: list[Attachment]):
    """현재 턴: 이미지/PDF는 네이티브 블록, 텍스트류는 본문에 첨부."""
    text = content
    blocks = []
    for att in attachments:
        if att.content_type in CLAUDE_IMAGE_TYPES and att.size_bytes <= MAX_IMAGE_BYTES:
            data = _read_file_bytes(att)
            if data:
                blocks.append({
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": att.content_type,
                        "data": base64.standard_b64encode(data).decode(),
                    },
                })
                continue
        if att.content_type == "application/pdf" and att.size_bytes <= MAX_PDF_BYTES:
            data = _read_file_bytes(att)
            if data:
                blocks.append({
                    "type": "document",
                    "source": {
                        "type": "base64",
                        "media_type": "application/pdf",
                        "data": base64.standard_b64encode(data).decode(),
                    },
                })
                continue
        text += _attachment_note(att, CURRENT_ATTACH_CHARS)

    blocks.append({"type": "text", "text": text})
    return blocks if len(blocks) > 1 else text


def build_openai_current_content(content: str, attachments: list[Attachment]):
    """가비아(OpenAI 호환) 현재 턴: 이미지는 data URI, 나머지는 텍스트 첨부."""
    text = content
    image_parts = []
    for att in attachments:
        if att.content_type.startswith("image/") and att.size_bytes <= MAX_IMAGE_BYTES:
            data = _read_file_bytes(att)
            if data:
                b64 = base64.standard_b64encode(data).decode()
                image_parts.append({
                    "type": "image_url",
                    "image_url": {"url": f"data:{att.content_type};base64,{b64}"},
                })
                continue
        text += _attachment_note(att, CURRENT_ATTACH_CHARS)

    if image_parts:
        return [{"type": "text", "text": text}] + image_parts
    return text


async def build_system_prompt(db: AsyncSession, project: Project | None) -> str | None:
    parts = []
    if project:
        if project.instructions and project.instructions.strip():
            parts.append(f"[프로젝트 지침]\n{project.instructions.strip()}")

        result = await db.execute(
            select(Attachment)
            .where(Attachment.project_id == project.id)
            .order_by(Attachment.id.asc())
        )
        total = 0
        knowledge = []
        for att in result.scalars().all():
            if not att.text_content:
                continue
            remain = PROJECT_TOTAL_CHARS - total
            if remain <= 0:
                break
            body = att.text_content[:min(PROJECT_FILE_CHARS, remain)]
            total += len(body)
            knowledge.append(f"<파일 이름=\"{att.filename}\">\n{body}\n</파일>")
        if knowledge:
            parts.append("[프로젝트 지식 파일]\n" + "\n\n".join(knowledge))

    # 사내 자료실 색인(프로젝트 유무와 무관하게 항상 안내 — 실제 내용은 도구로 검색)
    idx = await reference.cached_reference_index(db)
    if idx:
        parts.append(idx)

    return "\n\n".join(parts) if parts else None


def client_ip(request: Request) -> str:
    return getattr(request.state, "client_ip", None) or (request.client.host if request.client else "")


async def _load_attachments_map(db: AsyncSession, message_ids: list[int]) -> dict[int, list[Attachment]]:
    if not message_ids:
        return {}
    result = await db.execute(
        select(Attachment).where(Attachment.message_id.in_(message_ids)).order_by(Attachment.id.asc())
    )
    grouped: dict[int, list[Attachment]] = {}
    for att in result.scalars().all():
        grouped.setdefault(att.message_id, []).append(att)
    return grouped


async def _prepare_send(request: MessageCreate, http_request: Request,
                        current_user: User, db: AsyncSession) -> SimpleNamespace:
    """/send·/send/stream 공용 준비: 검증·대화·유저메시지 저장·히스토리·라우트·
    도구 컨텍스트 구성. finalize/make_log 클로저와 함께 컨텍스트를 돌려준다."""
    content = request.content.strip()
    if not content and not request.attachment_ids:
        raise HTTPException(status_code=400, detail="메시지 내용을 입력해주세요")

    # ---- 개인별 월 토큰 한도 초과 차단 (0/None = 무제한) ----
    limit = int(current_user.monthly_token_limit or 0)
    if limit > 0:
        used = await month_token_usage(db, current_user.id)
        if used >= limit:
            raise HTTPException(
                status_code=429,
                detail=(f"이번 달 토큰 한도({limit:,})를 모두 사용했습니다. "
                        f"관리자에게 한도 조정을 요청하세요."),
            )

    # ---- 첨부파일 검증 (내 소유 + 아직 미연결) ----
    attachments: list[Attachment] = []
    if request.attachment_ids:
        result = await db.execute(
            select(Attachment).where(Attachment.id.in_(request.attachment_ids))
        )
        found = {a.id: a for a in result.scalars().all()}
        for att_id in request.attachment_ids:
            att = found.get(att_id)
            if not att or att.user_id != current_user.id:
                raise HTTPException(status_code=404, detail="첨부파일을 찾을 수 없습니다")
            if att.message_id is not None or att.project_id is not None:
                raise HTTPException(status_code=400, detail="이미 사용된 첨부파일입니다")
            attachments.append(att)

    # ---- 대화 조회 또는 생성 ----
    project: Project | None = None
    if request.conversation_id:
        result = await db.execute(
            select(Conversation).where(
                Conversation.id == request.conversation_id,
                Conversation.user_id == current_user.id,
            )
        )
        conversation = result.scalar_one_or_none()
        if not conversation:
            raise HTTPException(status_code=404, detail="대화를 찾을 수 없습니다")
    else:
        if request.project_id:
            presult = await db.execute(
                select(Project).where(
                    Project.id == request.project_id,
                    Project.user_id == current_user.id,
                )
            )
            project = presult.scalar_one_or_none()
            if not project:
                raise HTTPException(status_code=404, detail="프로젝트를 찾을 수 없습니다")
        title_src = content or (attachments[0].filename if attachments else "새 대화")
        conversation = Conversation(
            user_id=current_user.id,
            project_id=project.id if project else None,
            title=title_src[:60] + ("..." if len(title_src) > 60 else ""),
        )
        db.add(conversation)
        await db.flush()

    if conversation.project_id and not project:
        presult = await db.execute(
            select(Project).where(
                Project.id == conversation.project_id,
                Project.user_id == current_user.id,  # 소유자 확인(방어적 — 타인 프로젝트 지침 주입 차단)
            )
        )
        project = presult.scalar_one_or_none()

    # ---- 사용자 메시지 저장 (AI 실패와 무관하게 보존되도록 먼저 커밋) ----
    user_msg = Message(conversation_id=conversation.id, role="user", content=content or "(첨부파일)")
    db.add(user_msg)
    await db.flush()
    for att in attachments:
        att.message_id = user_msg.id
    conversation.updated_at = datetime.utcnow()
    await db.commit()

    # ---- 히스토리 구성: '최근' 50개를 가져와 시간순으로 뒤집기 ----
    history_result = await db.execute(
        select(Message)
        .where(Message.conversation_id == conversation.id)
        .order_by(Message.created_at.desc(), Message.id.desc())
        .limit(50)
    )
    history = list(reversed(history_result.scalars().all()))
    attach_map = await _load_attachments_map(db, [m.id for m in history])

    requested = request.model or "sonnet"
    route = await get_model_route(db, requested, current_user)
    started = time.monotonic()
    ip = client_ip(http_request)

    def make_log(**kw) -> UsageLog:
        return UsageLog(
            user_id=current_user.id, username=current_user.username, action="chat",
            conversation_id=conversation.id, model=requested, provider=route.provider,
            client_ip=ip, duration_ms=int((time.monotonic() - started) * 1000), **kw,
        )

    async def finalize(assistant_content: str, *, in_tok=None, out_tok=None,
                       gen_atts=None, used_skills=None):
        """assistant 메시지 저장 + 생성물 첨부 연결 + 응답 구성(공통)."""
        assistant_msg = Message(
            conversation_id=conversation.id, role="assistant", content=assistant_content,
            model=requested, provider=route.provider,
        )
        db.add(assistant_msg)
        await db.flush()
        for att in (gen_atts or []):
            att.message_id = assistant_msg.id
        db.add(make_log(input_tokens=in_tok, output_tokens=out_tok))
        conversation.updated_at = datetime.utcnow()
        await db.commit()
        await db.refresh(assistant_msg)
        return ChatResponse(
            conversation_id=conversation.id,
            conversation_title=conversation.title,
            used_skills=used_skills or [],
            message=MessageResponse(
                id=assistant_msg.id, role=assistant_msg.role, content=assistant_msg.content,
                model=assistant_msg.model, provider=assistant_msg.provider,
                created_at=assistant_msg.created_at,
                attachments=[attachment_response(a) for a in (gen_atts or [])],
            ),
        )

    ctx = SimpleNamespace(
        conversation=conversation, requested=requested, route=route, content=content,
        make_log=make_log, finalize=finalize, kind=route.kind or "chat",
    )

    # ---- 이미지 생성 라우트: LLM 준비 없이 여기서 반환 ----
    if route.kind == "image":
        ctx.kind = "image"
        return ctx

    # ==================== 대화(+도구) 경로 준비 ====================
    ctx.kind = "chat"
    is_anthropic_family = route.provider in ANTHROPIC_FAMILY
    llm_messages = []
    for m in history:
        matts = attach_map.get(m.id, [])
        if m.id == user_msg.id:
            body = (build_claude_current_content(m.content, matts) if is_anthropic_family
                    else build_openai_current_content(m.content, matts))
        else:
            body = build_history_text(m, matts)
        llm_messages.append({"role": m.role, "content": body})

    system_prompt = await build_system_prompt(db, project)

    # 역할별 허용 스킬 + 내장 생성 도구(문서/엑셀)
    skills = await get_allowed_skills(db, current_user)
    skill_map = {s.name: s for s in skills}
    gen_atts: list[Attachment] = []

    async def tool_executor(name: str, params: dict) -> str:
        # 이미지 생성 도구(대화 중 "그려줘"): 모델을 바꾸지 않아도 자동으로 이미지 라우팅 호출
        if name == "generate_image":
            prompt = str(params.get("prompt") or "").strip()
            if not prompt:
                raise ValueError("이미지로 만들 내용을 알려주세요")
            img_route = None
            want = (settings.image_model_key or "").strip()
            if want:
                img_route = (await db.execute(select(ModelRoute).where(ModelRoute.key == want))).scalar_one_or_none()
            if not img_route or not img_route.enabled or (img_route.kind or "chat") != "image":
                rows = (await db.execute(select(ModelRoute).where(ModelRoute.enabled == True))).scalars().all()  # noqa: E712
                img_route = next((r for r in rows if (r.kind or "chat") == "image"), None)
            if not img_route:
                raise ValueError("이미지 생성 모델이 등록되어 있지 않습니다(관리자: 모델 라우팅에 kind=image 추가).")
            t0 = time.monotonic()
            log = UsageLog(user_id=current_user.id, username=current_user.username,
                           action="generate", conversation_id=conversation.id,
                           model=img_route.key, provider=img_route.provider, client_ip=ip)
            try:
                images = await run_image(db, img_route.provider, img_route.provider_model_id, prompt)
                base = gen_tools.safe_filename(prompt[:40], "생성이미지", "png")
                for i, img in enumerate(images):
                    stored = write_bytes(img.data, (img.content_type.split("/")[-1] or "png").split(";")[0][:5])
                    fn = base if len(images) == 1 else base.replace(".png", f"_{i+1}.png")
                    att = Attachment(user_id=current_user.id, filename=fn, content_type=img.content_type,
                                     size_bytes=len(img.data), stored_name=stored, kind="generated",
                                     origin="generate_image")
                    db.add(att); await db.flush(); gen_atts.append(att)
                log.duration_ms = int((time.monotonic() - t0) * 1000); db.add(log)
                return f"이미지를 생성했습니다({len(images)}장). 사용자 대화 화면에 바로 표시됩니다."
            except Exception as e:
                log.status = "error"; log.detail = str(e)[:500]
                log.duration_ms = int((time.monotonic() - t0) * 1000); db.add(log)
                raise
        # 내장 생성 도구
        if name in gen_tools.TOOLS:
            t0 = time.monotonic()
            log = UsageLog(user_id=current_user.id, username=current_user.username,
                           action="generate", conversation_id=conversation.id,
                           model=name, provider="builtin", client_ip=ip)
            try:
                data, ct, ext, fn = gen_tools.run_tool(name, params)
                stored = write_bytes(data, ext)
                att = Attachment(user_id=current_user.id, filename=fn, content_type=ct,
                                 size_bytes=len(data), stored_name=stored, kind="generated", origin=name)
                db.add(att)
                await db.flush()
                gen_atts.append(att)
                log.duration_ms = int((time.monotonic() - t0) * 1000); db.add(log)
                return f"'{fn}' 파일을 생성했습니다. 사용자가 대화 화면에서 바로 내려받을 수 있습니다."
            except Exception as e:
                log.status = "error"; log.detail = str(e)[:500]
                log.duration_ms = int((time.monotonic() - t0) * 1000); db.add(log)
                raise
        # 사내 자료실 검색(회사 참고자료에서 근거 찾기)
        if name == "search_company_docs":
            query = str(params.get("query") or "").strip()
            t0 = time.monotonic()
            log = UsageLog(user_id=current_user.id, username=current_user.username,
                           action="skill", conversation_id=conversation.id,
                           model="search_company_docs", provider="자료실", client_ip=ip,
                           request_params=query[:4000])
            docs = await reference.load_reference_docs(db)
            result = knowledge.search_reference(docs, query) if query else "검색어가 비어 있습니다."
            log.response_preview = result[:8000]
            log.duration_ms = int((time.monotonic() - t0) * 1000); db.add(log)
            return result
        # 외부 연동 스킬
        skill = skill_map.get(name)
        if not skill:
            raise ValueError(f"허용되지 않은 도구: {name}")
        t0 = time.monotonic()
        # 감사: AI가 보낸 요청 파라미터 기록(자격증명/헤더는 별도이며 저장하지 않음)
        try:
            req_str = json.dumps(params, ensure_ascii=False)[:4000]
        except (TypeError, ValueError):
            req_str = str(params)[:4000]
        log = UsageLog(user_id=current_user.id, username=current_user.username,
                       action="skill", conversation_id=conversation.id,
                       model=skill.name, provider=skill.integration.name, client_ip=ip,
                       request_params=req_str)
        try:
            result = await execute_skill(skill, skill.integration, params)
            log.response_preview = (result or "")[:8000]  # 외부에서 받은 데이터(요약 보관)
            log.duration_ms = int((time.monotonic() - t0) * 1000); db.add(log)
            return result
        except Exception as e:
            log.status = "error"; log.detail = str(e)[:500]
            log.duration_ms = int((time.monotonic() - t0) * 1000); db.add(log)
            raise

    titles = {s.name: s.title for s in skills}
    titles.update({"create_document": "문서 생성", "create_spreadsheet": "엑셀 생성",
                   "create_meeting_minutes": "회의록(HP 양식) 생성", "generate_image": "이미지 생성",
                   "create_estimate": "견적서(HP 양식) 생성",
                   "create_transaction_statement": "거래명세서(HP 양식) 생성",
                   "create_overtime_request": "연장근로신청서(HP 양식) 생성",
                   "create_expense_report": "지출결의서(HP 양식) 생성",
                   "search_company_docs": "사내 자료실 검색"})
    # 사내 자료실에 자료가 있으면 검색 도구 노출
    extra_anth, extra_oai = [], []
    if await reference.has_reference_docs(db):
        extra_anth.append(SEARCH_COMPANY_DOCS_ANTHROPIC)
        extra_oai.append(SEARCH_COMPANY_DOCS_OPENAI)
    ctx.llm_messages = llm_messages
    ctx.system_prompt = system_prompt
    ctx.gen_atts = gen_atts
    ctx.tool_ctx = ToolContext(
        anthropic_tools=gen_tools.anthropic_defs() + extra_anth + [anthropic_tool_def(s) for s in skills],
        openai_tools=gen_tools.openai_defs() + extra_oai + [openai_tool_def(s) for s in skills],
        titles=titles,
        executor=tool_executor,
    )
    return ctx


async def _generate_images(ctx: SimpleNamespace, current_user: User, db: AsyncSession) -> list[Attachment]:
    """이미지 라우트 실행 → 생성물 Attachment 목록. 실패 시 로그 남기고 예외."""
    if not ctx.content:
        raise HTTPException(status_code=400, detail="이미지로 만들 내용을 입력해주세요")
    try:
        images = await run_image(db, ctx.route.provider, ctx.route.provider_model_id, ctx.content)
    except HTTPException as e:
        db.add(ctx.make_log(status="error", detail=str(e.detail)[:500])); await db.commit(); raise
    except Exception as e:
        detail = friendly_api_error(e)
        db.add(ctx.make_log(status="error", detail=detail[:500])); await db.commit()
        raise HTTPException(status_code=502, detail=detail)
    gen_atts = []
    base = gen_tools.safe_filename(ctx.content[:40], "생성이미지", "png")
    for i, img in enumerate(images):
        stored = write_bytes(img.data, (img.content_type.split("/")[-1] or "png").split(";")[0][:5])
        fn = base if len(images) == 1 else base.replace(".png", f"_{i+1}.png")
        att = Attachment(user_id=current_user.id, filename=fn, content_type=img.content_type,
                         size_bytes=len(img.data), stored_name=stored, kind="generated", origin=ctx.requested)
        db.add(att); gen_atts.append(att)
    return gen_atts


@router.post("/send", response_model=ChatResponse)
async def send_message(
    request: MessageCreate,
    http_request: Request,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    ctx = await _prepare_send(request, http_request, current_user, db)

    if ctx.kind == "image":
        gen_atts = await _generate_images(ctx, current_user, db)
        return await ctx.finalize("요청하신 이미지를 생성했어요.", gen_atts=gen_atts,
                                  used_skills=[{"name": ctx.requested, "title": ctx.route.label, "status": "success"}])

    try:
        outcome = await run_chat(db, ctx.route.provider, ctx.route.provider_model_id,
                                 ctx.system_prompt, ctx.llm_messages, ctx.tool_ctx)
    except HTTPException as e:
        db.add(ctx.make_log(status="error", detail=str(e.detail)[:500])); await db.commit(); raise
    except Exception as e:
        detail = friendly_api_error(e)
        db.add(ctx.make_log(status="error", detail=detail[:500])); await db.commit()
        raise HTTPException(status_code=502, detail=detail)

    return await ctx.finalize(
        outcome.text, in_tok=outcome.input_tokens, out_tok=outcome.output_tokens,
        gen_atts=ctx.gen_atts,
        used_skills=[{"name": s.name, "title": s.title, "status": s.status} for s in outcome.used_skills],
    )


def _sse(obj: dict) -> str:
    return f"data: {json.dumps(obj, ensure_ascii=False)}\n\n"


@router.post("/send/stream")
async def send_message_stream(
    request: MessageCreate,
    http_request: Request,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """SSE 스트리밍 응답. 이벤트: start / delta / tool / skill / done / error."""
    ctx = await _prepare_send(request, http_request, current_user, db)

    async def gen():
        yield _sse({"type": "start", "conversation_id": ctx.conversation.id,
                    "conversation_title": ctx.conversation.title})
        try:
            if ctx.kind == "image":
                try:
                    gen_atts = await _generate_images(ctx, current_user, db)
                except HTTPException as e:
                    yield _sse({"type": "error", "detail": str(e.detail)}); return
                resp = await ctx.finalize("요청하신 이미지를 생성했어요.", gen_atts=gen_atts,
                                          used_skills=[{"name": ctx.requested, "title": ctx.route.label, "status": "success"}])
                yield _sse({"type": "done", **resp.model_dump(mode="json")})
                return

            outcome = None
            try:
                async for kind, payload in run_chat_stream(
                        db, ctx.route.provider, ctx.route.provider_model_id,
                        ctx.system_prompt, ctx.llm_messages, ctx.tool_ctx):
                    if kind == "delta":
                        yield _sse({"type": "delta", "text": payload})
                    elif kind == "tool":
                        yield _sse({"type": "tool", "title": payload})
                    elif kind == "skill":
                        yield _sse({"type": "skill", "name": payload.name,
                                    "title": payload.title, "status": payload.status})
                    elif kind == "done":
                        outcome = payload
            except HTTPException as e:
                db.add(ctx.make_log(status="error", detail=str(e.detail)[:500])); await db.commit()
                yield _sse({"type": "error", "detail": str(e.detail)}); return
            except Exception as e:
                detail = friendly_api_error(e)
                db.add(ctx.make_log(status="error", detail=detail[:500])); await db.commit()
                yield _sse({"type": "error", "detail": detail}); return

            if outcome is None:
                yield _sse({"type": "error", "detail": "빈 응답"}); return
            resp = await ctx.finalize(
                outcome.text, in_tok=outcome.input_tokens, out_tok=outcome.output_tokens,
                gen_atts=ctx.gen_atts,
                used_skills=[{"name": s.name, "title": s.title, "status": s.status} for s in outcome.used_skills],
            )
            yield _sse({"type": "done", **resp.model_dump(mode="json")})
        except Exception as e:
            yield _sse({"type": "error", "detail": friendly_api_error(e)})

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.get("/models")
async def list_models(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """현재 사용자가 쓸 수 있는 모델 목록(관리자 패널의 라우팅 설정 반영)."""
    result = await db.execute(
        select(ModelRoute).where(ModelRoute.enabled == True).order_by(ModelRoute.sort.asc(), ModelRoute.id.asc())
    )
    routes = [r for r in result.scalars().all() if has_min_role(current_user, r.min_role)]
    return [
        {
            "key": r.key,
            "label": r.label,
            "provider": r.provider,
            "provider_label": PROVIDERS.get(r.provider, {}).get("label", r.provider),
            "description": r.description or "",
            "kind": r.kind or "chat",
        }
        for r in routes
    ]


@router.get("/conversations", response_model=list[ConversationResponse])
async def get_conversations(
    archived: bool = False,
    project_id: int | None = None,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    query = (
        select(Conversation)
        .where(
            Conversation.user_id == current_user.id,
            Conversation.is_archived == archived,
        )
        .order_by(Conversation.updated_at.desc())
    )
    if project_id is not None:
        query = query.where(Conversation.project_id == project_id)
    result = await db.execute(query.limit(1000))  # 자기 데이터 폭증에 대한 상한
    return result.scalars().all()


async def _get_own_conversation(conversation_id: int, user: User, db: AsyncSession) -> Conversation:
    result = await db.execute(
        select(Conversation).where(
            Conversation.id == conversation_id,
            Conversation.user_id == user.id,
        )
    )
    conversation = result.scalar_one_or_none()
    if not conversation:
        raise HTTPException(status_code=404, detail="대화를 찾을 수 없습니다")
    return conversation


@router.get("/conversations/{conversation_id}/messages", response_model=list[MessageResponse])
async def get_messages(
    conversation_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await _get_own_conversation(conversation_id, current_user, db)

    result = await db.execute(
        select(Message)
        .where(Message.conversation_id == conversation_id)
        .order_by(Message.created_at.asc(), Message.id.asc())
        .limit(5000)  # 단일 대화 표시 상한(자기 데이터 폭증 방지)
    )
    messages = result.scalars().all()
    attach_map = await _load_attachments_map(db, [m.id for m in messages])
    return [
        MessageResponse(
            id=m.id, role=m.role, content=m.content,
            model=m.model, provider=m.provider, created_at=m.created_at,
            attachments=[attachment_response(a) for a in attach_map.get(m.id, [])],
        )
        for m in messages
    ]


@router.patch("/conversations/{conversation_id}", response_model=ConversationResponse)
async def update_conversation(
    conversation_id: int,
    request: ConversationUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    conversation = await _get_own_conversation(conversation_id, current_user, db)
    if request.title is not None:
        title = request.title.strip()
        if not title:
            raise HTTPException(status_code=400, detail="제목을 입력해주세요")
        conversation.title = title[:255]
    await db.commit()
    await db.refresh(conversation)
    return conversation


@router.patch("/conversations/{conversation_id}/archive", response_model=ConversationResponse)
async def toggle_archive(
    conversation_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    conversation = await _get_own_conversation(conversation_id, current_user, db)
    conversation.is_archived = not conversation.is_archived
    await db.commit()
    await db.refresh(conversation)
    return conversation


@router.get("/conversations/{conversation_id}/export")
async def export_conversation(
    conversation_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """대화를 마크다운 파일로 내보내기(저장 기능)."""
    conversation = await _get_own_conversation(conversation_id, current_user, db)
    result = await db.execute(
        select(Message)
        .where(Message.conversation_id == conversation_id)
        .order_by(Message.created_at.asc(), Message.id.asc())
        .limit(5000)  # 내보내기 상한(대용량 메모리 폭증 방지)
    )
    messages = result.scalars().all()
    attach_map = await _load_attachments_map(db, [m.id for m in messages])

    lines = [
        f"# {conversation.title}",
        "",
        f"- 내보낸 사용자: {current_user.username}",
        f"- 생성: {conversation.created_at:%Y-%m-%d %H:%M} UTC",
        f"- 내보내기: {datetime.utcnow():%Y-%m-%d %H:%M} UTC",
        f"- 메시지 수: {len(messages)}",
        "",
        "---",
        "",
    ]
    for m in messages:
        if m.role == "user":
            header = f"## 🙋 사용자 ({m.created_at:%m-%d %H:%M})"
        else:
            model_note = f" · {m.model}" if m.model else ""
            header = f"## 🤖 AI{model_note} ({m.created_at:%m-%d %H:%M})"
        lines.append(header)
        for att in attach_map.get(m.id, []):
            lines.append(f"> 📎 첨부: {att.filename} ({att.size_bytes:,} bytes)")
        lines.append("")
        lines.append(m.content)
        lines.append("")

    safe_title = "".join(c for c in conversation.title if c.isalnum() or c in " -_")[:40].strip() or "대화"
    filename = f"{safe_title}_{conversation.id}.md"
    # HTTP 헤더는 latin-1만 허용되므로 한글 파일명은 RFC 5987 percent-encoding
    return PlainTextResponse(
        "\n".join(lines),
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}"},
    )


@router.delete("/conversations/{conversation_id}")
async def delete_conversation(
    conversation_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    import os
    conversation = await _get_own_conversation(conversation_id, current_user, db)

    # 디스크 파일 정리를 위해 첨부 stored_name 수집
    result = await db.execute(
        select(Attachment.stored_name)
        .join(Message, Attachment.message_id == Message.id)
        .where(Message.conversation_id == conversation_id)
    )
    stored_names = [row[0] for row in result.all()]

    await db.delete(conversation)
    await db.commit()

    for name in stored_names:
        path = stored_path(name)
        if os.path.isfile(path):
            try:
                os.remove(path)
            except OSError:
                pass
    return {"message": "대화가 삭제되었습니다"}


def _month_start() -> datetime:
    now = datetime.utcnow()
    return datetime(now.year, now.month, 1)


async def month_token_usage(db: AsyncSession, user_id: int) -> int:
    """이번 달 토큰 총사용량(한도 판정용). 대화뿐 아니라 이미지 생성·스킬 등
    토큰을 소비하는 모든 성공 호출을 합산해 한도 우회를 막는다."""
    r = await db.execute(
        select(func.coalesce(func.sum(UsageLog.input_tokens), 0)
               + func.coalesce(func.sum(UsageLog.output_tokens), 0))
        .where(UsageLog.user_id == user_id, UsageLog.status == "success",
               UsageLog.created_at >= _month_start())
    )
    return int(r.scalar() or 0)


@router.get("/usage/me")
async def my_usage(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """이번 달 내 토큰 사용량(사이드바 표시용). 한도가 설정돼 있으면 함께 반환."""
    now = datetime.utcnow()
    result = await db.execute(
        select(
            func.coalesce(func.sum(UsageLog.input_tokens), 0),
            func.coalesce(func.sum(UsageLog.output_tokens), 0),
            func.count(UsageLog.id),
        ).where(
            UsageLog.user_id == current_user.id,
            UsageLog.action == "chat",
            UsageLog.status == "success",
            UsageLog.created_at >= _month_start(),
        )
    )
    in_tok, out_tok, count = result.one()
    # 한도는 모든 토큰 사용(대화+이미지+스킬)을 합산하므로, 게이지 total도 동일 기준으로 맞춘다.
    total = await month_token_usage(db, current_user.id)
    return {
        "month": f"{now.year}-{now.month:02d}",
        "input_tokens": int(in_tok),
        "output_tokens": int(out_tok),
        "total_tokens": total,
        "chat_count": int(count),
        "limit": int(current_user.monthly_token_limit or 0),
    }
