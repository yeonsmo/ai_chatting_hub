"""사내 자료실 — 회사 참고자료(배관 규격·중량·플랜지표 등)를 보관하고,
대화 중 AI가 검색해 근거로 답하게 한다.

- 업로드/삭제는 최고관리자만. 목록 조회는 전 직원(내용은 대화에서 AI가 검색해 사용).
- 저장은 Attachment(kind='reference', 소유자=최고관리자). 추출 텍스트를 text_content에 보관.
- 서버 기동 시 app/assets/reference/ 의 기본 자료를 1회 시드(파일명 기준 중복 방지).
"""
import os
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Request
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.database import get_db
from app.dependencies import get_current_user, get_superadmin_user
from app.file_utils import new_stored_name, stored_path
from app.models import User, Attachment
from app import knowledge
from app.routers.files import read_upload_or_413

router = APIRouter(prefix="/reference", tags=["reference"])

_SEED_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "assets", "reference")


def _item(att: Attachment) -> dict:
    return {"id": att.id, "filename": att.filename, "size_bytes": att.size_bytes,
            "sections": knowledge.sheet_titles(att.text_content or "")[:12],
            "created_at": att.created_at.isoformat() if att.created_at else None}


async def has_reference_docs(db: AsyncSession) -> bool:
    from sqlalchemy import func
    n = await db.scalar(select(func.count(Attachment.id)).where(Attachment.kind == "reference"))
    return bool(n)


async def load_reference_docs(db: AsyncSession) -> list[tuple[str, str]]:
    """검색용 (파일명, 추출텍스트) 목록. 텍스트가 있는 자료만."""
    rows = (await db.execute(
        select(Attachment).where(Attachment.kind == "reference").order_by(Attachment.id.asc())
    )).scalars().all()
    return [(a.filename, a.text_content) for a in rows if a.text_content]


def reference_index(docs: list[tuple[str, str]]) -> str:
    """시스템 프롬프트에 넣을 자료실 색인(자료명 + 구획)."""
    if not docs:
        return ""
    lines = [f"- {f} (구획: {', '.join(knowledge.sheet_titles(t)[:8]) or '문서'})" for f, t in docs]
    return ("[사내 자료실]\n회사 기술·규격 자료가 자료실에 있다. 배관·규격·치수·중량·플랜지·볼트·"
            "재질·인장강도 등 자료로 답할 수 있는 질문이면 반드시 search_company_docs 도구로 "
            "해당 자료를 찾아 그 값을 근거로 답하라. 자료에 없으면 지어내지 말고 없다고 하라.\n"
            "보유 자료:\n" + "\n".join(lines))


# 색인은 메시지마다 필요하나 자료는 드물게 바뀌므로, (건수, 최대 id) 키로 캐시한다.
_INDEX_CACHE = {"key": None, "text": ""}


async def cached_reference_index(db: AsyncSession) -> str:
    """자료실 색인 문자열. 자료 집합이 바뀌지 않으면 캐시를 재사용(전체 텍스트 재적재 방지)."""
    from sqlalchemy import func
    row = (await db.execute(
        select(func.count(Attachment.id), func.max(Attachment.id)).where(Attachment.kind == "reference")
    )).first()
    key = (row[0] or 0, row[1] or 0)
    if _INDEX_CACHE["key"] != key:
        _INDEX_CACHE["key"] = key
        _INDEX_CACHE["text"] = reference_index(await load_reference_docs(db)) if key[0] else ""
    return _INDEX_CACHE["text"]


@router.get("")
async def list_reference(
    _: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """자료실 목록(전 직원). 내용 본문은 포함하지 않는다."""
    rows = (await db.execute(
        select(Attachment).where(Attachment.kind == "reference").order_by(Attachment.id.desc())
    )).scalars().all()
    return [_item(a) for a in rows]


@router.post("")
async def upload_reference(
    request: Request,
    file: UploadFile = File(...),
    _: User = Depends(get_superadmin_user),
    db: AsyncSession = Depends(get_db),
):
    """자료실에 파일 추가(최고관리자). 엑셀은 시트별로, 그 외는 텍스트 추출해 검색 대상에 포함."""
    data = await read_upload_or_413(request, file)
    if not data:
        raise HTTPException(status_code=400, detail="빈 파일은 올릴 수 없습니다")
    filename = os.path.basename(file.filename or "자료")[:255]
    content_type = (file.content_type or "application/octet-stream")[:120]
    text = knowledge.extract_reference_text(data, filename, content_type)
    if not text:
        raise HTTPException(status_code=400,
                            detail="이 파일에서 텍스트를 추출하지 못했습니다(엑셀/PDF/텍스트 지원).")
    stored = new_stored_name(filename)
    with open(stored_path(stored), "wb") as f:
        f.write(data)
    att = Attachment(user_id=(await get_superadmin_id(db)), filename=filename,
                     content_type=content_type, size_bytes=len(data), stored_name=stored,
                     text_content=text, kind="reference", origin="자료실")
    db.add(att)
    await db.commit()
    await db.refresh(att)
    return _item(att)


@router.delete("/{ref_id}")
async def delete_reference(
    ref_id: int,
    _: User = Depends(get_superadmin_user),
    db: AsyncSession = Depends(get_db),
):
    att = (await db.execute(
        select(Attachment).where(Attachment.id == ref_id, Attachment.kind == "reference")
    )).scalar_one_or_none()
    if not att:
        raise HTTPException(status_code=404, detail="자료를 찾을 수 없습니다")
    path = stored_path(att.stored_name)
    await db.delete(att)
    await db.commit()
    if os.path.isfile(path):
        try:
            os.remove(path)
        except OSError:
            pass
    return {"message": "삭제되었습니다"}


async def get_superadmin_id(db: AsyncSession) -> int:
    from app.config import settings
    from app.models import UserRole
    u = (await db.execute(
        select(User).where(User.username == settings.superadmin_username)
    )).scalar_one_or_none()
    if u is None:
        u = (await db.execute(
            select(User).where(User.role == UserRole.superadmin).order_by(User.id.asc())
        )).scalars().first()
    if u is None:
        raise HTTPException(status_code=500, detail="자료 소유자(최고관리자)를 찾을 수 없습니다")
    return u.id


async def seed_reference_docs(db: AsyncSession):
    """기본 번들 자료를 1회 시드. 이미 같은 파일명이 있으면 건너뛴다."""
    if not os.path.isdir(_SEED_DIR):
        return
    existing = set((await db.execute(
        select(Attachment.filename).where(Attachment.kind == "reference")
    )).scalars().all())
    try:
        owner_id = await get_superadmin_id(db)
    except HTTPException:
        return  # 최고관리자 없으면 시드 보류
    added = 0
    for name in sorted(os.listdir(_SEED_DIR)):
        if name in existing:
            continue
        src = os.path.join(_SEED_DIR, name)
        if not os.path.isfile(src):
            continue
        with open(src, "rb") as f:
            data = f.read()
        ct = ("application/vnd.ms-excel" if name.lower().endswith(".xls")
              else "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
              if name.lower().endswith(".xlsx") else "application/octet-stream")
        text = knowledge.extract_reference_text(data, name, ct)
        if not text:
            continue
        stored = new_stored_name(name)
        with open(stored_path(stored), "wb") as f:
            f.write(data)
        db.add(Attachment(user_id=owner_id, filename=name, content_type=ct,
                          size_bytes=len(data), stored_name=stored, text_content=text,
                          kind="reference", origin="기본자료"))
        added += 1
    if added:
        await db.commit()
        print(f"[INIT] 사내 자료실 기본자료 {added}건 시드 완료")
