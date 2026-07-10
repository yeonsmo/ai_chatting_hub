"""작업 평가(피드백) — 산출물이 나온 대화를 마칠 때 강제로 받는다.

- 대상: 최고관리자를 제외한 전 직원.
- 트리거(프론트): 산출물(생성 문서/이미지 등)이 있는 대화에서 '다음 작업으로' 넘어갈 때
  아직 평가하지 않았다면 강제 팝업 → 👍/👎 + 이유(한글 15자+) 제출해야 진행.
- 목적: 실사용 불편/만족 경험을 양질의 학습셋 큐레이션 신호로 축적.
"""
import re

from fastapi import APIRouter, Depends, HTTPException, Body
from sqlalchemy import select, and_, exists
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies import get_current_user, get_superadmin_user
from app.models import User, UserRole, Conversation, Message, Attachment, Feedback

router = APIRouter(prefix="/feedback", tags=["feedback"])

_HANGUL = re.compile(r"[가-힣]")
_MIN_REASON = 15


def _valid_reason(s: str) -> bool:
    """한글 포함 + 공백 제외 15자 이상."""
    s = (s or "").strip()
    nospace = re.sub(r"\s+", "", s)
    return len(nospace) >= _MIN_REASON and bool(_HANGUL.search(s))


async def _pending_conversation(db: AsyncSession, user: User):
    """산출물이 있고 아직 이 사용자가 평가하지 않은 가장 오래된 대화."""
    deliv = (select(Message.conversation_id)
             .join(Attachment, Attachment.message_id == Message.id)
             .where(Attachment.kind == "generated"))
    q = (select(Conversation)
         .where(Conversation.user_id == user.id,
                Conversation.id.in_(deliv),
                ~exists().where(and_(Feedback.conversation_id == Conversation.id,
                                     Feedback.user_id == user.id)))
         .order_by(Conversation.created_at.asc())
         .limit(1))
    return (await db.execute(q)).scalar_one_or_none()


@router.get("/pending")
async def pending(current_user: User = Depends(get_current_user),
                  db: AsyncSession = Depends(get_db)):
    """평가가 필요한 대화 1건(없으면 null). 최고관리자는 항상 null(면제)."""
    if current_user.role == UserRole.superadmin:
        return {"pending": None}
    conv = await _pending_conversation(db, current_user)
    if not conv:
        return {"pending": None}
    return {"pending": {"conversation_id": conv.id, "title": conv.title or "제목 없는 작업"}}


@router.post("")
async def submit(payload: dict = Body(...),
                 current_user: User = Depends(get_current_user),
                 db: AsyncSession = Depends(get_db)):
    """평가 저장(대화당 1건, 재제출 시 갱신). 이유는 한글 15자+ 강제."""
    cid = payload.get("conversation_id")
    rating = str(payload.get("rating") or "")
    reason = str(payload.get("reason") or "")
    if rating not in ("up", "down"):
        raise HTTPException(status_code=400, detail="평가를 선택해주세요(좋아요/아쉬워요)")
    if not _valid_reason(reason):
        raise HTTPException(status_code=400, detail="평가 이유를 한글로 15자 이상 작성해주세요")
    conv = (await db.execute(
        select(Conversation).where(Conversation.id == cid, Conversation.user_id == current_user.id)
    )).scalar_one_or_none()
    if not conv:
        raise HTTPException(status_code=404, detail="대화를 찾을 수 없습니다")
    existing = (await db.execute(
        select(Feedback).where(Feedback.conversation_id == cid, Feedback.user_id == current_user.id)
    )).scalar_one_or_none()
    if existing:
        existing.rating = rating
        existing.reason = reason.strip()[:4000]
    else:
        db.add(Feedback(conversation_id=cid, user_id=current_user.id,
                        username=current_user.username, rating=rating,
                        reason=reason.strip()[:4000]))
    await db.commit()
    return {"ok": True}


@router.get("")
async def list_feedback(limit: int = 200, offset: int = 0,
                        _: User = Depends(get_superadmin_user),
                        db: AsyncSession = Depends(get_db)):
    """수집된 평가 열람(최고관리자) — 학습셋 큐레이션/분석용."""
    limit = max(1, min(limit, 500))
    rows = (await db.execute(
        select(Feedback, Conversation.title)
        .join(Conversation, Conversation.id == Feedback.conversation_id)
        .order_by(Feedback.created_at.desc())
        .limit(limit).offset(max(0, offset))
    )).all()
    return [{"id": f.id, "conversation_id": f.conversation_id, "title": title or "",
             "username": f.username, "rating": f.rating, "reason": f.reason,
             "created_at": f.created_at.isoformat() if f.created_at else None}
            for f, title in rows]
