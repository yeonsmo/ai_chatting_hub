"""생성 서류 결재 워크플로우 — 기안(HR 전송)/보류(1일 후 자동삭제).

- 대상: 허브에서 생성된 HR/결재 서류(지출결의서·연장근로신청서·휴가신청서·유연근무신청서).
- 기안: 본인 사번을 실어 HR 결재로 전송 → HR이 승인 워크플로우 진행.
- 보류: 보관함에 유지하되 24시간 뒤 자동 삭제(기안 안 하면).
"""
import os
import secrets
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
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
    "create_family_care_leave": "family_care_leave",
    "create_early_leave": "early_leave",
    # 개인보호구 지급확인서(상용/일용)는 허브 결재 대상 아님(일용은 별도 일용직 HR 모듈로 처리).
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
    # 접수 직후는 '결재중'. 최종 결과(승인/반려)가 오면 그때 팝업으로 알린다.
    att.approval_status = hr_sync._norm_approval(result.get("approval_status")) or "pending"
    att.approval_note = None
    att.approval_seen = (att.approval_status == "pending")  # 결재중은 알림 불필요
    await db.commit()
    return {"status": "submitted", "hr_ref": att.hr_ref,
            "approval_status": att.approval_status}


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


def _apply_status(att: Attachment, status: str, note: str | None) -> bool:
    """결재 결과를 첨부에 반영. 최종상태(승인/반려)로 '바뀔 때'만 알림 대상으로 표시.
    반환: 새 알림이 생겼는지."""
    status = hr_sync._norm_approval(status) or ""
    if status not in ("approved", "rejected"):
        # pending 등은 상태만 갱신(알림 없음)
        if status and att.approval_status != status:
            att.approval_status = status
        return False
    if att.approval_status == status:
        return False                     # 이미 같은 결과 → 중복 알림 방지
    att.approval_status = status
    att.approval_note = (str(note)[:300] if note else None)
    att.approval_seen = False            # 사용자에게 팝업으로 알릴 대상
    return True


@router.post("/hr-callback")
async def hr_approval_callback(payload: dict, request: Request,
                               db: AsyncSession = Depends(get_db)):
    """HR → 허브 승인/반려 콜백(push). 공유 토큰(HR_CALLBACK_TOKEN)으로 인증.
    body: {document_id|hr_ref|id, status: approved|rejected, note?}."""
    token = (settings.hr_callback_token or "").strip()
    if not token:
        raise HTTPException(status_code=404, detail="콜백이 활성화되지 않았습니다")
    sent = (request.headers.get("X-HR-Callback-Token")
            or request.headers.get("Authorization", "").replace("Bearer ", "")).strip()
    if not sent or not secrets.compare_digest(sent, token):
        raise HTTPException(status_code=401, detail="콜백 인증 실패")
    ref = str(payload.get("document_id") or payload.get("hr_ref")
              or payload.get("id") or "").strip()
    status = payload.get("status") or payload.get("approval_status")
    if not ref or not status:
        raise HTTPException(status_code=400, detail="document_id와 status가 필요합니다")
    att = (await db.execute(
        select(Attachment).where(Attachment.hr_ref == ref,
                                 Attachment.workflow_status == "submitted"))).scalars().first()
    if not att:
        raise HTTPException(status_code=404, detail="해당 기안 문서를 찾을 수 없습니다")
    changed = _apply_status(att, status, payload.get("note") or payload.get("reason"))
    await db.commit()
    return {"ok": True, "changed": changed, "approval_status": att.approval_status}


@router.get("/notifications")
async def approval_notifications(current_user: User = Depends(get_current_user),
                                 db: AsyncSession = Depends(get_db)):
    """본인 기안 서류 중 아직 확인 안 한 결재 결과(승인/반려) 목록 → 프론트 팝업용."""
    rows = (await db.execute(
        select(Attachment).where(
            Attachment.user_id == current_user.id,
            Attachment.approval_seen.is_(False),
            Attachment.approval_status.in_(["approved", "rejected"]),
        ).order_by(Attachment.id.desc()))).scalars().all()
    return {"notifications": [
        {"id": a.id, "filename": a.filename, "status": a.approval_status,
         "note": a.approval_note, "hr_ref": a.hr_ref} for a in rows]}


@router.post("/notifications/ack")
async def ack_notifications(payload: dict, current_user: User = Depends(get_current_user),
                            db: AsyncSession = Depends(get_db)):
    """팝업으로 보여준 결재 결과를 '확인함' 처리(다시 안 뜨게)."""
    ids = payload.get("ids")
    q = select(Attachment).where(Attachment.user_id == current_user.id,
                                 Attachment.approval_seen.is_(False))
    if isinstance(ids, list) and ids:
        q = q.where(Attachment.id.in_([int(i) for i in ids if str(i).isdigit()]))
    rows = (await db.execute(q)).scalars().all()
    for a in rows:
        a.approval_seen = True
    if rows:
        await db.commit()
    return {"acked": len(rows)}


async def poll_submitted_documents(db: AsyncSession) -> int:
    """기안됐지만 아직 결과가 안 온 서류들의 HR 결재 상태를 폴링해 갱신.
    hr_document_status_path 미설정이면 아무것도 하지 않는다. 반환: 새 알림 건수."""
    if not (settings.hr_document_status_path or "").strip() or not hr_sync.hr_configured():
        return 0
    rows = (await db.execute(
        select(Attachment).where(
            Attachment.workflow_status == "submitted",
            Attachment.hr_ref.isnot(None),
            (Attachment.approval_status.is_(None)) | (Attachment.approval_status == "pending"),
        ))).scalars().all()
    new = 0
    for att in rows:
        try:
            res = await hr_sync.fetch_document_status(att.hr_ref)
        except Exception:  # noqa: BLE001 — 개별 실패가 루프를 멈추지 않도록
            res = None
        if res and _apply_status(att, res["status"], res.get("note")):
            new += 1
    if new:
        await db.commit()
    return new


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
