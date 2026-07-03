import base64
import time
from datetime import datetime
from urllib.parse import quote
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import PlainTextResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from app.database import get_db
from app.dependencies import get_current_user
from app.file_utils import stored_path
from app.models import (User, Conversation, Message, Attachment, Project, UsageLog,
                        ModelRoute, Skill, Integration)
from app.providers import (run_chat, friendly_api_error, ToolContext,
                           ANTHROPIC_FAMILY, PROVIDERS)
from app.roles import role_level, has_min_role
from app.routers.files import to_response as attachment_response
from app.skills_runtime import execute_skill, anthropic_tool_def, openai_tool_def
from app.schemas import MessageCreate, ChatResponse, ConversationResponse, ConversationUpdate, MessageResponse

router = APIRouter(prefix="/chat", tags=["chat"])

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
    if not project:
        return None
    parts = []
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


@router.post("/send", response_model=ChatResponse)
async def send_message(
    request: MessageCreate,
    http_request: Request,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    content = request.content.strip()
    if not content and not request.attachment_ids:
        raise HTTPException(status_code=400, detail="메시지 내용을 입력해주세요")

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

    # ---- 스킬(도구) 컨텍스트: 역할별 허용 스킬만 노출 ----
    started = time.monotonic()
    skills = await get_allowed_skills(db, current_user)
    skill_map = {s.name: s for s in skills}
    ip = client_ip(http_request)

    async def skill_executor(name: str, params: dict) -> str:
        skill = skill_map.get(name)
        if not skill:
            raise ValueError(f"허용되지 않은 스킬: {name}")
        t0 = time.monotonic()
        log = UsageLog(
            user_id=current_user.id, username=current_user.username,
            action="skill", conversation_id=conversation.id,
            model=skill.name, provider=skill.integration.name,
            client_ip=ip,
        )
        try:
            result = await execute_skill(skill, skill.integration, params)
            log.duration_ms = int((time.monotonic() - t0) * 1000)
            db.add(log)
            return result
        except Exception as e:
            log.status = "error"
            log.detail = str(e)[:500]
            log.duration_ms = int((time.monotonic() - t0) * 1000)
            db.add(log)
            raise

    tool_ctx = ToolContext(
        anthropic_tools=[anthropic_tool_def(s) for s in skills],
        openai_tools=[openai_tool_def(s) for s in skills],
        titles={s.name: s.title for s in skills},
        executor=skill_executor,
    ) if skills else None

    def make_log(**kw) -> UsageLog:
        return UsageLog(
            user_id=current_user.id,
            username=current_user.username,
            action="chat",
            conversation_id=conversation.id,
            model=requested,
            provider=route.provider,
            client_ip=ip,
            duration_ms=int((time.monotonic() - started) * 1000),
            **kw,
        )

    try:
        outcome = await run_chat(db, route.provider, route.provider_model_id,
                                 system_prompt, llm_messages, tool_ctx)
        assistant_content = outcome.text
        in_tok, out_tok = outcome.input_tokens, outcome.output_tokens
    except HTTPException as e:
        db.add(make_log(status="error", detail=str(e.detail)[:500]))
        await db.commit()
        raise
    except Exception as e:
        detail = friendly_api_error(e)
        db.add(make_log(status="error", detail=detail[:500]))
        await db.commit()
        raise HTTPException(status_code=502, detail=detail)

    # ---- 응답 저장 ----
    assistant_msg = Message(
        conversation_id=conversation.id,
        role="assistant",
        content=assistant_content,
        model=requested,
        provider=route.provider,
    )
    db.add(assistant_msg)
    db.add(make_log(input_tokens=in_tok, output_tokens=out_tok))
    conversation.updated_at = datetime.utcnow()
    await db.commit()
    await db.refresh(assistant_msg)

    return ChatResponse(
        conversation_id=conversation.id,
        conversation_title=conversation.title,
        used_skills=[
            {"name": s.name, "title": s.title, "status": s.status}
            for s in outcome.used_skills
        ],
        message=MessageResponse(
            id=assistant_msg.id,
            role=assistant_msg.role,
            content=assistant_msg.content,
            model=assistant_msg.model,
            provider=assistant_msg.provider,
            created_at=assistant_msg.created_at,
        ),
    )


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


@router.get("/usage/me")
async def my_usage(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """이번 달 내 토큰 사용량(사이드바 표시용)."""
    now = datetime.utcnow()
    month_start = datetime(now.year, now.month, 1)
    result = await db.execute(
        select(
            func.coalesce(func.sum(UsageLog.input_tokens), 0),
            func.coalesce(func.sum(UsageLog.output_tokens), 0),
            func.count(UsageLog.id),
        ).where(
            UsageLog.user_id == current_user.id,
            UsageLog.action == "chat",
            UsageLog.status == "success",
            UsageLog.created_at >= month_start,
        )
    )
    in_tok, out_tok, count = result.one()
    return {
        "month": f"{now.year}-{now.month:02d}",
        "input_tokens": int(in_tok),
        "output_tokens": int(out_tok),
        "total_tokens": int(in_tok) + int(out_tok),
        "chat_count": int(count),
    }
