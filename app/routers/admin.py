from datetime import datetime, timedelta
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from app.database import get_db
from app.dependencies import get_superadmin_user
from app.models import User, Conversation, Message, UsageLog
from app.routers.chat import _load_attachments_map
from app.routers.files import to_response as attachment_response
from app.schemas import AdminConversationResponse, MessageResponse, UsageLogResponse

router = APIRouter(prefix="/admin", tags=["admin"])


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
