"""회의 녹음 전사 API.

흐름: 녹음 업로드 → 원본을 첨부로 보관(증빙) → 백그라운드에서 전사(faster-whisper) →
클라이언트가 job_id로 진행상태를 폴링 → 완료 시 전사 텍스트 반환.

- 원본 녹음은 kind='recording' 첨부로 영구 보관(수동 삭제 전까지). 다운로드는 소유자와
  최고관리자만 가능(_get_accessible 규칙 재사용).
- 전사 텍스트는 해당 첨부의 text_content에도 저장해 기록으로 남긴다.
- 잡 상태는 인메모리(재시작 시 소멸)지만 녹음/전사는 DB에 남으므로 유실되지 않는다.
"""
import asyncio
import os
import uuid

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Request
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.database import get_db, AsyncSessionLocal
from app.dependencies import get_current_user
from app.file_utils import new_stored_name, stored_path
from app.models import User, UserRole, Attachment
from app.routers.files import read_upload_or_413, to_response
from app import transcribe

router = APIRouter(prefix="/minutes", tags=["minutes"])

# job_id -> {user_id, status: pending|done|error, transcript, error, attachment_id}
_JOBS: dict = {}
_JOBS_MAX = 500

_AUDIO_EXTS = {".mp3", ".m4a", ".wav", ".webm", ".ogg", ".aac", ".flac", ".mp4", ".mpga", ".oga", ".amr"}


def _looks_audio(filename: str, content_type: str) -> bool:
    if (content_type or "").lower().startswith(("audio/", "video/")):
        return True
    ext = os.path.splitext(filename or "")[1].lower()
    return ext in _AUDIO_EXTS


def _prune_jobs():
    if len(_JOBS) > _JOBS_MAX:
        for k in [k for k, v in list(_JOBS.items()) if v.get("status") in ("done", "error")][:100]:
            _JOBS.pop(k, None)


async def _run_job(job_id: str, path: str, att_id: int):
    """백그라운드 전사. 완료/실패를 잡 상태에 기록하고 전사문을 첨부에 보존."""
    try:
        text = await transcribe.transcribe_async(path)
        _JOBS[job_id]["transcript"] = text
        _JOBS[job_id]["status"] = "done"
        # 전사문을 첨부 기록에 저장(증빙/후속 참조용). 실패해도 잡 결과에는 영향 없음.
        try:
            async with AsyncSessionLocal() as db:
                att = (await db.execute(select(Attachment).where(Attachment.id == att_id))).scalar_one_or_none()
                if att is not None:
                    att.text_content = text[:100_000]
                    await db.commit()
        except Exception:
            pass
    except transcribe.TranscribeUnavailable as e:
        _JOBS[job_id]["status"] = "error"; _JOBS[job_id]["error"] = str(e)
    except Exception as e:  # noqa: BLE001
        _JOBS[job_id]["status"] = "error"
        _JOBS[job_id]["error"] = f"전사 중 오류가 발생했습니다 ({type(e).__name__})"


@router.post("/transcribe")
async def start_transcribe(
    request: Request,
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """회의 녹음 업로드 → 보관 + 전사 작업 시작. {job_id, attachment} 반환."""
    filename = os.path.basename(file.filename or "recording")[:255]
    content_type = (file.content_type or "application/octet-stream")[:120]
    if not _looks_audio(filename, content_type):
        raise HTTPException(status_code=400, detail="오디오(녹음) 파일만 전사할 수 있습니다")

    data = await read_upload_or_413(request, file)
    if not data:
        raise HTTPException(status_code=400, detail="빈 파일은 업로드할 수 없습니다")

    stored = new_stored_name(filename)
    with open(stored_path(stored), "wb") as f:
        f.write(data)

    att = Attachment(
        user_id=current_user.id,
        filename=filename,
        content_type=content_type,
        size_bytes=len(data),
        stored_name=stored,
        kind="recording",
        origin="meeting",
    )
    db.add(att)
    await db.commit()
    await db.refresh(att)

    job_id = uuid.uuid4().hex
    _prune_jobs()
    _JOBS[job_id] = {"user_id": current_user.id, "status": "pending",
                     "transcript": "", "error": "", "attachment_id": att.id}
    asyncio.create_task(_run_job(job_id, stored_path(stored), att.id))
    return {"job_id": job_id, "attachment": to_response(att)}


@router.get("/transcribe/{job_id}")
async def transcribe_status(
    job_id: str,
    current_user: User = Depends(get_current_user),
):
    """전사 진행상태 폴링. status: pending|done|error."""
    job = _JOBS.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="전사 작업을 찾을 수 없습니다(만료되었을 수 있습니다)")
    if job["user_id"] != current_user.id and current_user.role != UserRole.superadmin:
        raise HTTPException(status_code=403, detail="권한이 없습니다")
    return {"status": job["status"], "transcript": job.get("transcript", ""),
            "error": job.get("error", ""), "attachment_id": job.get("attachment_id")}
