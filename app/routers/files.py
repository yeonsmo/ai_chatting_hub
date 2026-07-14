import os
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Request
from fastapi.responses import FileResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from app.config import settings
from app.database import get_db
from app.dependencies import get_current_user
from app.file_utils import new_stored_name, stored_path, extract_text, read_capped, UploadTooLarge
from app.models import User, UserRole, Attachment
from app.schemas import AttachmentResponse

router = APIRouter(prefix="/files", tags=["files"])


async def read_upload_or_413(request: Request, file: UploadFile) -> bytes:
    """Content-Length 사전 거부 + 청크 스트리밍 캡으로 안전하게 업로드 본문을 읽는다."""
    max_bytes = settings.max_upload_mb * 1024 * 1024
    cl = request.headers.get("content-length")
    if cl and cl.isdigit() and int(cl) > max_bytes + 65536:
        raise HTTPException(status_code=413, detail=f"파일이 너무 큽니다 (최대 {settings.max_upload_mb}MB)")
    try:
        return await read_capped(file, max_bytes)
    except UploadTooLarge:
        raise HTTPException(status_code=413, detail=f"파일이 너무 큽니다 (최대 {settings.max_upload_mb}MB)")


def to_response(att: Attachment) -> AttachmentResponse:
    return AttachmentResponse(
        id=att.id,
        filename=att.filename,
        content_type=att.content_type,
        size_bytes=att.size_bytes,
        has_text=bool(att.text_content),
        kind=att.kind or "upload",
        origin=att.origin,
        workflow_status=att.workflow_status,
        approval_status=att.approval_status,
        created_at=att.created_at,
    )


@router.post("/upload", response_model=AttachmentResponse)
async def upload_file(
    request: Request,
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """어떤 형식이든 업로드 허용. 텍스트/PDF는 내용을 추출해 모델 컨텍스트로 사용."""
    data = await read_upload_or_413(request, file)
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


@router.get("/generated", response_model=list[AttachmentResponse])
async def list_generated(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """내가 만든 생성물(문서·이미지) 보관함 — 최신순."""
    result = await db.execute(
        select(Attachment)
        .where(Attachment.user_id == current_user.id, Attachment.kind.in_(["generated", "recording"]))
        .order_by(Attachment.id.desc())
        .limit(500)
    )
    return [to_response(a) for a in result.scalars().all()]


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


@router.get("/{att_id}/preview")
async def preview_file(
    att_id: int,
    page: int = 1,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """PDF 첨부의 지정 페이지를 PNG 이미지로 렌더해 반환(미리보기용). 다운로드와 동일한 접근권한."""
    att = await _get_accessible(att_id, current_user, db)
    path = stored_path(att.stored_name)
    if not os.path.isfile(path):
        raise HTTPException(status_code=410, detail="파일이 저장소에서 삭제되었습니다")
    is_pdf = "pdf" in (att.content_type or "").lower() or att.filename.lower().endswith(".pdf")
    if not is_pdf:
        raise HTTPException(status_code=400, detail="미리보기는 PDF 문서만 지원합니다")
    try:
        import fitz  # PyMuPDF
        doc = fitz.open(path)
        try:
            total_pages = doc.page_count
            idx = min(max(page - 1, 0), total_pages - 1)
            png = doc[idx].get_pixmap(dpi=130).tobytes("png")
        finally:
            doc.close()
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=500, detail="미리보기를 만들 수 없습니다")
    return Response(content=png, media_type="image/png",
                    headers={"Cache-Control": "private, max-age=300",
                             "X-Total-Pages": str(total_pages),
                             "Access-Control-Expose-Headers": "X-Total-Pages"})


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
