from datetime import datetime, timedelta
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, case
from app.database import get_db
from app.dependencies import get_superadmin_user
from app.models import User, Conversation, Message, UsageLog, Attachment, Feedback
from app.routers.chat import _load_attachments_map, _month_start
from app.routers.files import to_response as attachment_response
from app.schemas import AdminConversationResponse, MessageResponse, UsageLogResponse

router = APIRouter(prefix="/admin", tags=["admin"])


@router.get("/utilization")
async def utilization(
    days: int = 90,
    _: User = Depends(get_superadmin_user),
    db: AsyncSession = Depends(get_db),
):
    """직원별 AI 활용도 지표(최고관리자) — 인사평가/도입효과 분석용.
    최근 N일 기준: 대화 수, 내가 보낸 메시지 수, 산출물 수, 활동일수, 도구·스킬 사용,
    최근 활동, 남긴 평가(👍/👎), 토큰 사용량."""
    days = max(1, min(days, 3650))
    since = datetime.utcnow() - timedelta(days=days)

    # 사용자 목록(최고관리자 본인 포함해 전원). 삭제된 사용자는 로그 username으로만 남음.
    users = (await db.execute(select(User).order_by(User.id.asc()))).scalars().all()

    async def _by_user(stmt):
        return {row[0]: row[1:] for row in (await db.execute(stmt)).all()}

    convs = await _by_user(
        select(Conversation.user_id, func.count(Conversation.id))
        .where(Conversation.created_at >= since).group_by(Conversation.user_id))
    # 내가 보낸 메시지 수(활동량) — Message는 conversation 통해 user 연결
    msgs = await _by_user(
        select(Conversation.user_id, func.count(Message.id))
        .join(Message, Message.conversation_id == Conversation.id)
        .where(Message.role == "user", Message.created_at >= since)
        .group_by(Conversation.user_id))
    delivs = await _by_user(
        select(Attachment.user_id, func.count(Attachment.id))
        .where(Attachment.kind == "generated", Attachment.created_at >= since)
        .group_by(Attachment.user_id))
    # 활동일수 / 최근활동 / 토큰 — UsageLog 기준
    act = await _by_user(
        select(UsageLog.user_id,
               func.count(func.distinct(func.date(UsageLog.created_at))),
               func.max(UsageLog.created_at),
               func.coalesce(func.sum(UsageLog.input_tokens), 0),
               func.coalesce(func.sum(UsageLog.output_tokens), 0))
        .where(UsageLog.created_at >= since, UsageLog.action.in_(["chat", "skill", "generate"]))
        .group_by(UsageLog.user_id))
    tools = await _by_user(
        select(UsageLog.user_id, func.count(UsageLog.id))
        .where(UsageLog.created_at >= since, UsageLog.action.in_(["skill", "generate"]))
        .group_by(UsageLog.user_id))
    fbs = await _by_user(
        select(Feedback.user_id, func.count(Feedback.id),
               func.coalesce(func.sum(case((Feedback.rating == "up", 1), else_=0)), 0))
        .where(Feedback.created_at >= since).group_by(Feedback.user_id))
    # 이번 달 토큰(한도 대비 표시용) — 활용도 기간(N일)과 별개로 월 단위 집계.
    # 한도가 모든 토큰 사용을 합산하므로 여기서도 action 필터 없이 전량 집계(일관성).
    month_tok = await _by_user(
        select(UsageLog.user_id,
               func.coalesce(func.sum(UsageLog.input_tokens), 0)
               + func.coalesce(func.sum(UsageLog.output_tokens), 0))
        .where(UsageLog.status == "success",
               UsageLog.created_at >= _month_start()).group_by(UsageLog.user_id))

    rows = []
    for u in users:
        a = act.get(u.id, (0, None, 0, 0))
        active_days, last_at, in_tok, out_tok = a[0], a[1], a[2], a[3]
        fb = fbs.get(u.id, (0, 0))
        rows.append({
            "user_id": u.id, "username": u.username, "role": u.role.value if hasattr(u.role, "value") else str(u.role),
            "name": u.name or "", "department": u.department or "", "position": u.position or "",
            "employee_no": u.employee_no or "",
            "conversations": int(convs.get(u.id, (0,))[0] or 0),
            "messages": int(msgs.get(u.id, (0,))[0] or 0),
            "deliverables": int(delivs.get(u.id, (0,))[0] or 0),
            "tool_uses": int(tools.get(u.id, (0,))[0] or 0),
            "active_days": int(active_days or 0),
            "tokens": int((in_tok or 0) + (out_tok or 0)),
            "feedback_count": int(fb[0] or 0),
            "feedback_up": int(fb[1] or 0),
            "last_active": last_at.isoformat() if last_at else None,
            "monthly_token_limit": int(u.monthly_token_limit or 0),
            "month_tokens": int(month_tok.get(u.id, (0,))[0] or 0),
        })
    # 활동량(메시지) 순 정렬
    rows.sort(key=lambda r: (r["messages"], r["deliverables"]), reverse=True)
    return {"days": days, "users": rows}


@router.get("/conversations", response_model=list[AdminConversationResponse])
async def all_conversations(
    user_id: int | None = None,
    q: str | None = None,
    limit: int = 100,
    offset: int = 0,
    _: User = Depends(get_superadmin_user),
    db: AsyncSession = Depends(get_db),
):
    """전 직원 대화 목록 조회 (최고 관리자 전용)."""
    limit = max(1, min(limit, 200))
    msg_count = (
        select(func.count(Message.id))
        .where(Message.conversation_id == Conversation.id)
        .scalar_subquery()
    )
    query = (
        select(Conversation, User.username, msg_count)
        .join(User, User.id == Conversation.user_id)
        .order_by(Conversation.updated_at.desc())
        .limit(limit)
        .offset(max(0, offset))
    )
    if user_id is not None:
        query = query.where(Conversation.user_id == user_id)
    if q:
        query = query.where(Conversation.title.ilike(f"%{q.strip()}%"))

    result = await db.execute(query)
    return [
        AdminConversationResponse(
            id=c.id, title=c.title, user_id=c.user_id, username=username,
            project_id=c.project_id, is_archived=c.is_archived,
            message_count=int(count or 0),
            created_at=c.created_at, updated_at=c.updated_at,
        )
        for c, username, count in result.all()
    ]


@router.get("/conversations/{conversation_id}/messages", response_model=list[MessageResponse])
async def conversation_messages(
    conversation_id: int,
    _: User = Depends(get_superadmin_user),
    db: AsyncSession = Depends(get_db),
):
    """특정 대화의 메시지 열람 (최고 관리자 전용)."""
    exists = await db.scalar(select(Conversation.id).where(Conversation.id == conversation_id))
    if not exists:
        raise HTTPException(status_code=404, detail="대화를 찾을 수 없습니다")

    result = await db.execute(
        select(Message)
        .where(Message.conversation_id == conversation_id)
        .order_by(Message.created_at.asc(), Message.id.asc())
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


@router.get("/logs", response_model=list[UsageLogResponse])
async def usage_logs(
    user_id: int | None = None,
    action: str | None = None,
    days: int | None = None,
    limit: int = 100,
    offset: int = 0,
    _: User = Depends(get_superadmin_user),
    db: AsyncSession = Depends(get_db),
):
    """언제 누가 어떤 모델을 사용했는지 감사 로그 (최고 관리자 전용)."""
    limit = max(1, min(limit, 500))
    query = select(UsageLog).order_by(UsageLog.id.desc()).limit(limit).offset(max(0, offset))
    if user_id is not None:
        query = query.where(UsageLog.user_id == user_id)
    if action:
        query = query.where(UsageLog.action == action)
    if days:
        query = query.where(UsageLog.created_at >= datetime.utcnow() - timedelta(days=max(1, days)))
    result = await db.execute(query)
    return result.scalars().all()


@router.get("/logs/summary")
async def usage_summary(
    days: int = 30,
    _: User = Depends(get_superadmin_user),
    db: AsyncSession = Depends(get_db),
):
    """사용자별 사용량 요약 (최고 관리자 전용)."""
    since = datetime.utcnow() - timedelta(days=max(1, min(days, 365)))
    result = await db.execute(
        select(
            UsageLog.username,
            func.count(UsageLog.id),
            func.coalesce(func.sum(UsageLog.input_tokens), 0),
            func.coalesce(func.sum(UsageLog.output_tokens), 0),
            func.max(UsageLog.created_at),
        )
        .where(UsageLog.action == "chat", UsageLog.created_at >= since)
        .group_by(UsageLog.username)
        .order_by(func.count(UsageLog.id).desc())
    )
    return [
        {
            "username": username,
            "chat_count": int(count),
            "input_tokens": int(in_tok),
            "output_tokens": int(out_tok),
            "last_used": last_used,
        }
        for username, count, in_tok, out_tok, last_used in result.all()
    ]


class TokenLimitBody(BaseModel):
    monthly_token_limit: int = 0  # 0 = 무제한


@router.post("/users/{user_id}/token-limit")
async def set_token_limit(
    user_id: int,
    body: TokenLimitBody,
    _: User = Depends(get_superadmin_user),
    db: AsyncSession = Depends(get_db),
):
    """사용자 개인별 월 토큰 한도 설정(최고관리자). 0/음수는 무제한(None)으로 저장."""
    user = (await db.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="사용자를 찾을 수 없습니다")
    limit = int(body.monthly_token_limit or 0)
    user.monthly_token_limit = limit if limit > 0 else None
    await db.commit()
    return {"user_id": user.id, "monthly_token_limit": int(user.monthly_token_limit or 0)}


@router.get("/usage/{user_id}")
async def user_usage_detail(
    user_id: int,
    _: User = Depends(get_superadmin_user),
    db: AsyncSession = Depends(get_db),
):
    """특정 사용자의 이번 달 사용 상세(최고관리자) — 어디에 사용 비중이 높은지.
    모델별·용도별(대화/스킬/생성) 토큰·횟수 분해."""
    user = (await db.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="사용자를 찾을 수 없습니다")
    since = _month_start()

    tok = (func.coalesce(func.sum(UsageLog.input_tokens), 0)
           + func.coalesce(func.sum(UsageLog.output_tokens), 0))
    base = (UsageLog.user_id == user_id, UsageLog.created_at >= since)

    # 모델별(대화 토큰 기준)
    by_model = (await db.execute(
        select(UsageLog.model, func.count(UsageLog.id), tok)
        .where(*base, UsageLog.action == "chat", UsageLog.status == "success")
        .group_by(UsageLog.model).order_by(tok.desc()))).all()
    # 용도별(action) — 대화/스킬/생성 등 어디에 많이 쓰는지. 성공 호출만(한도·총합과 동일 기준)
    by_action = (await db.execute(
        select(UsageLog.action, func.count(UsageLog.id), tok)
        .where(*base, UsageLog.status == "success")
        .group_by(UsageLog.action).order_by(func.count(UsageLog.id).desc()))).all()
    # 생성 산출물 유형별(origin) — 견적서/회의록 등
    by_origin = (await db.execute(
        select(Attachment.origin, func.count(Attachment.id))
        .where(Attachment.user_id == user_id, Attachment.kind == "generated",
               Attachment.created_at >= since)
        .group_by(Attachment.origin).order_by(func.count(Attachment.id).desc()))).all()

    total = int((await db.execute(
        select(tok).where(*base, UsageLog.action == "chat",
                          UsageLog.status == "success"))).scalar() or 0)
    now = datetime.utcnow()
    return {
        "user_id": user.id, "username": user.username,
        "name": user.name or "", "department": user.department or "",
        "month": f"{now.year}-{now.month:02d}",
        "monthly_token_limit": int(user.monthly_token_limit or 0),
        "total_tokens": total,
        "by_model": [{"model": m or "(미지정)", "count": int(c), "tokens": int(t)}
                     for m, c, t in by_model],
        "by_action": [{"action": a or "(미지정)", "count": int(c), "tokens": int(t)}
                      for a, c, t in by_action],
        "by_origin": [{"origin": o or "(미지정)", "count": int(c)} for o, c in by_origin],
    }
