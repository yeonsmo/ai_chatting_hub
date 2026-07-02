import os
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from fastapi.responses import FileResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from app.config import settings
from app.database import get_db
from app.dependencies import get_current_user
from app.file_utils import new_stored_name, stored_path, extract_text
from app.models import User, UserRole, Attachment
from app.schemas import AttachmentResponse

router = APIRouter(prefix="/files", tags=["files"])


def to_response(att: Attachment) -> AttachmentResponse:
    return AttachmentResponse(
        id=att.id,
        filename=att.filename,
        content_type=att.content_type,
        size_bytes=att.size_bytes,
        has_text=bool(att.text_content),
        created_at=att.created_at,
    )


@router.post("/upload", response_model=AttachmentResponse)
async def upload_file(
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """어떤 형식이든 업로드 허용. 텍스트/PDF는 내용을 추출해 모델 컨텍스트로 사용."""
    max_bytes = settings.max_upload_mb * 1024 * 1024
    data = await file.read()
    if len(data) > max_bytes:
        raise HTTPException(status_code=413, detail=f"파일이 너무 큽니다 (최대 {settings.max_upload_mb}MB)")
    if not data:
        raise HTTPException(status_code=400, detail="빈 파일은 업로드할 수 없습니다")

    filename = os.path.basename(file.filename or "파일")[:255]
    content_type = (file.content_type or "application/octet-stream")[:120]

    stored = new_stored_name(filename)
    with open(stored_path(stored), "wb") as f:
        f.write(data)

    att = Attachment(
        user_id=current_user.id,
        filename=filename,
        content_type=content_type,
        size_bytes=len(data),
        stored_name=stored,
        text_content=extract_text(data, filename, content_type),
    )
    db.add(att)
    await db.commit()
    await db.refresh(att)
    return to_response(att)


async def _get_accessible(att_id: int, user: User, db: AsyncSession) -> Attachment:
    result = await db.execute(select(Attachment).where(Attachment.id == att_id))
    att = result.scalar_one_or_none()
    if not att:
        raise HTTPException(status_code=404, detail="파일을 찾을 수 없습니다")
    if att.user_id != user.id and user.role != UserRole.superadmin:
        raise HTTPException(status_code=403, detail="권한이 없습니다")
    return att


@router.get("/{att_id}/download")
async def download_file(
    att_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    att = await _get_accessible(att_id, current_user, db)
    path = stored_path(att.stored_name)
    if not os.path.isfile(path):
        raise HTTPException(status_code=410, detail="파일이 저장소에서 삭제되었습니다")
    return FileResponse(path, filename=att.filename, media_type=att.content_type)


@router.delete("/{att_id}")
async def delete_file(
    att_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """아직 메시지에 연결되지 않은(작성 중 취소한) 첨부만 삭제 가능."""
    att = await _get_accessible(att_id, current_user, db)
    if att.message_id is not None:
        raise HTTPException(status_code=400, detail="이미 메시지에 첨부된 파일은 삭제할 수 없습니다")
    path = stored_path(att.stored_name)
    await db.delete(att)
    await db.commit()
    if os.path.isfile(path):
        try:
            os.remove(path)
        except OSError:
            pass
    return {"message": "삭제되었습니다"}
