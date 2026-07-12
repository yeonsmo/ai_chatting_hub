"""생성 서류 결재 워크플로우 — 기안(HR 전송)/보류(1일 후 자동삭제).

- 대상: 허브에서 생성된 HR/결재 서류(지출결의서·연장근로신청서·휴가신청서·유연근무신청서).
- 기안: 본인 사번을 실어 HR 결재로 전송 → HR이 승인 워크플로우 진행.
- 보류: 보관함에 유지하되 24시간 뒤 자동 삭제(기안 안 하면).
"""
import os
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies import get_current_user
from app.file_utils import stored_path
from app.models import User, Attachment
from app import hr_sync
from app.routers.files import to_response

router = APIRouter(prefix="/documents", tags=["documents"])

HOLD_TTL = timedelta(days=1)

# 생성 도구(origin) → HR 문서유형. 여기 없는 서류는 기안 대상이 아님(견적서 등은 별도 연동).
HR_DOC_TYPES = {
    "create_expense_report": "expense_report",
    "create_overtime_request": "overtime_request",
    "create_leave_request": "leave_request",
    "create_flexible_work_request": "flexible_work_request",
}


async def _own_doc(att_id: int, user: User, db: AsyncSession) -> Attachment:
    att = (await db.execute(select(Attachment).where(Attachment.id == att_id))).scalar_one_or_none()
    if not att or att.user_id != user.id:
        raise HTTPException(status_code=404, detail="문서를 찾을 수 없습니다")
    if (att.kind or "") != "generated":
        raise HTTPException(status_code=400, detail="생성된 서류만 기안/보류할 수 있습니다")
    return att


@router.post("/{att_id}/submit")
async def submit_document(att_id: int, current_user: User = Depends(get_current_user),
                          db: AsyncSession = Depends(get_db)):
    """기안 — 본인 사번을 실어 HR 결재로 전송."""
    att = await _own_doc(att_id, current_user, db)
    doc_type = HR_DOC_TYPES.get(att.origin or "")
    if not doc_type:
        raise HTTPException(status_code=400, detail="이 서류는 HR 기안 대상이 아닙니다")
    if att.workflow_status == "submitted":
        raise HTTPException(status_code=400, detail="이미 기안된 문서입니다")
    if not hr_sync.hr_configured():
        raise HTTPException(status_code=503, detail="HR 연동이 설정되지 않았습니다")
    path = stored_path(att.stored_name)
    if not os.path.isfile(path):
        raise HTTPException(status_code=410, detail="파일이 저장소에서 삭제되었습니다")
    with open(path, "rb") as f:
        data = f.read()
    try:
        result = await hr_sync.submit_document(current_user, doc_type, att.filename, data,
                                               att.filename, att.content_type or "application/pdf")
    except hr_sync.HRSyncError as e:
        raise HTTPException(status_code=502, detail=str(e))
    att.workflow_status = "submitted"
    att.hr_ref = str(result.get("hr_document_id") or "")[:120] or None
    att.expires_at = None
    await db.commit()
    return {"status": "submitted", "hr_ref": att.hr_ref,
            "approval_status": result.get("approval_status")}


@router.post("/{att_id}/hold")
async def hold_document(att_id: int, current_user: User = Depends(get_current_user),
                        db: AsyncSession = Depends(get_db)):
    """보류 — 보관함에 두되 24시간 뒤 자동 삭제(기안 전까지)."""
    att = await _own_doc(att_id, current_user, db)
    if att.workflow_status == "submitted":
        raise HTTPException(status_code=400, detail="이미 기안된 문서는 보류할 수 없습니다")
    att.workflow_status = "held"
    att.expires_at = datetime.utcnow() + HOLD_TTL
    await db.commit()
    return {"status": "held", "expires_at": att.expires_at.isoformat()}


@router.post("/{att_id}/resume")
async def resume_document(att_id: int, current_user: User = Depends(get_current_user),
                          db: AsyncSession = Depends(get_db)):
    """보류 해제 — 자동삭제 예약 취소(다시 기안/보류 선택 가능)."""
    att = await _own_doc(att_id, current_user, db)
    if att.workflow_status == "held":
        att.workflow_status = None
        att.expires_at = None
        await db.commit()
    return {"status": "active"}


async def purge_expired_held(db: AsyncSession) -> int:
    """만료된 보류 문서 삭제(파일+행). 반환: 삭제 건수."""
    now = datetime.utcnow()
    rows = (await db.execute(
        select(Attachment).where(Attachment.workflow_status == "held",
                                 Attachment.expires_at.isnot(None),
                                 Attachment.expires_at < now)
    )).scalars().all()
    n = 0
    for att in rows:
        path = stored_path(att.stored_name)
        await db.delete(att)
        n += 1
        if os.path.isfile(path):
            try:
                os.remove(path)
            except OSError:
                pass
    if n:
        await db.commit()
    return n
