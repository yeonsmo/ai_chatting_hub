"""회의 녹음 전사 API.

흐름: 녹음 업로드 → 원본을 첨부로 보관(증빙) → 백그라운드에서 전사(faster-whisper) →
클라이언트가 job_id로 진행상태를 폴링 → 완료 시 전사 텍스트 반환.

- 원본 녹음은 kind='recording' 첨부로 영구 보관(수동 삭제 전까지). 다운로드는 소유자와
  최고관리자만 가능(_get_accessible 규칙 재사용).
- 전사 텍스트는 해당 첨부의 text_content에도 저장해 기록으로 남긴다.
- 잡 상태는 인메모리(재시작 시 소멸)지만 녹음/전사는 DB에 남으므로 유실되지 않는다.
"""
import asyncio
import json
import os
import re
import uuid

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Request, Body
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.config import settings
from app.database import get_db, AsyncSessionLocal
from app.dependencies import get_current_user
from app.file_utils import new_stored_name, stored_path
from app.models import User, UserRole, Attachment, ModelRoute
from app.routers.files import read_upload_or_413, to_response
from app import transcribe, doc_gen, gen_tools
from app.providers import run_chat, OPENAI_FAMILY, friendly_api_error

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


# ---------------- 회의록 생성 (구조화 출력, 모델 고정) ----------------

async def _resolve_minutes_route(db: AsyncSession) -> ModelRoute:
    """회의록 정리용 모델 라우팅 결정. 설정 key(기본 gpt-5) → 없으면 GPT 계열(빠른 것 우선) →
    그래도 없으면 아무 채팅 라우팅으로 대체. 도구 호출이 아니라 구조화 출력이므로
    어떤 채팅 모델이든 동작한다."""
    want = (settings.minutes_model_key or "").strip()
    if want:
        r = (await db.execute(select(ModelRoute).where(ModelRoute.key == want))).scalar_one_or_none()
        if r and r.enabled and (r.kind or "chat") == "chat":
            return r
    rows = (await db.execute(select(ModelRoute).where(ModelRoute.enabled == True))).scalars().all()  # noqa: E712
    chats = [c for c in rows if (c.kind or "chat") == "chat"]
    gpts = [c for c in chats if c.provider in OPENAI_FAMILY and "gpt" in (c.key or "").lower()]
    if gpts:
        # 속도 우선: 'pro'/'codex'(느린 계열)는 뒤로, 나머지 먼저
        gpts.sort(key=lambda c: (("pro" in c.key.lower() or "codex" in c.key.lower()), c.key))
        return gpts[0]
    if chats:
        return chats[0]
    raise HTTPException(status_code=500, detail="회의록 생성용 AI 모델이 등록되어 있지 않습니다. 관리자에게 문의하세요.")


def _extract_json_obj(text: str) -> dict:
    """모델 응답에서 JSON 객체 추출(코드펜스/앞뒤 잡텍스트 허용)."""
    t = (text or "").strip()
    if t.startswith("```"):
        t = re.sub(r"^```[a-zA-Z]*", "", t).strip()
        if t.endswith("```"):
            t = t[:-3].strip()
    i, j = t.find("{"), t.rfind("}")
    if i >= 0 and j > i:
        t = t[i:j + 1]
    return json.loads(t)


@router.post("/generate")
async def generate_minutes(
    payload: dict = Body(...),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """회의 메타 + 메모/전사문 → (고정 모델의 구조화 출력) → 회사 표준 양식 회의록 PDF.
    도구 호출에 의존하지 않아 어떤 모델(가비아 GPT Pro 포함)에서도 안정적으로 생성된다."""
    notes = str(payload.get("notes") or "").strip()
    if not notes:
        raise HTTPException(status_code=400, detail="회의 내용(메모/전사)을 입력해주세요")
    title = str(payload.get("title") or "").strip()

    route = await _resolve_minutes_route(db)
    system_prompt = ("너는 회의록 정리 보조자다. 반드시 유효한 JSON 객체 하나만 출력한다. "
                     "코드펜스(```), 설명, 인사말을 절대 붙이지 마라.")
    user_msg = (
        "다음 회의 메모/전사문을 아래 JSON 스키마로만 정리하라.\n"
        '스키마: {"purpose": "회의 목적 한 줄", '
        '"content": "회의 내용 본문(핵심 위주 번호매김/문단, 줄바꿈은 \\n)", '
        '"notes": ["특기사항·결정사항·액션아이템 항목", "... 최대 9개"]}\n'
        "규칙: 메모에 실제로 있는 사실만 사용하고 지어내지 마라. 없으면 빈 문자열/빈 배열로 둔다.\n\n"
        f"[회의 제목/목적] {title or '(미입력)'}\n"
        f"[회의 메모/전사]\n{notes}"
    )
    messages = [{"role": "user", "content": user_msg}]
    try:
        outcome = await run_chat(db, route.provider, route.provider_model_id,
                                 system_prompt, messages, None)
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=friendly_api_error(e))

    try:
        parsed = _extract_json_obj(outcome.text)
    except (ValueError, TypeError):
        parsed = {"purpose": title, "content": notes, "notes": []}  # 파싱 실패 시 원문 보존

    fields = {
        "datetime": payload.get("datetime"), "place": payload.get("place"),
        "dept": payload.get("dept"), "writer": payload.get("writer"),
        "ext_company": payload.get("ext_company"),
        "inside": payload.get("inside"), "outside": payload.get("outside"),
        "mgmt_no": payload.get("mgmt_no"), "approval_no": payload.get("approval_no"),
        "purpose": parsed.get("purpose") or title,
        "content": parsed.get("content") or notes,
        "notes": parsed.get("notes") or [],
    }
    data = doc_gen.render_minutes_pdf(fields)
    fn = gen_tools.safe_filename(title or "회의록", "회의록", "pdf")

    stored = new_stored_name(fn)
    with open(stored_path(stored), "wb") as f:
        f.write(data)
    att = Attachment(
        user_id=current_user.id, filename=fn, content_type=doc_gen.PDF_CT,
        size_bytes=len(data), stored_name=stored, kind="generated", origin="회의록",
    )
    db.add(att)
    await db.commit()
    await db.refresh(att)
    return {"attachment": to_response(att),
            "input_tokens": outcome.input_tokens, "output_tokens": outcome.output_tokens,
            "model": route.label}
