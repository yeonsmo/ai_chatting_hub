import io
import os
import re
import zipfile
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Request
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from app.config import settings
from app.database import get_db
from app.dependencies import get_current_user
from app.file_utils import new_stored_name, stored_path, extract_text
from app.models import User, Project, Conversation, Attachment
from app.routers.files import to_response as attachment_response, read_upload_or_413
from app.schemas import ProjectCreate, ProjectUpdate, ProjectResponse, AttachmentResponse

router = APIRouter(prefix="/projects", tags=["projects"])


async def _get_own_project(project_id: int, user: User, db: AsyncSession) -> Project:
    result = await db.execute(
        select(Project).where(Project.id == project_id, Project.user_id == user.id)
    )
    project = result.scalar_one_or_none()
    if not project:
        raise HTTPException(status_code=404, detail="프로젝트를 찾을 수 없습니다")
    return project


async def _to_response(project: Project, db: AsyncSession) -> ProjectResponse:
    fcount = await db.scalar(
        select(func.count(Attachment.id)).where(Attachment.project_id == project.id)
    )
    ccount = await db.scalar(
        select(func.count(Conversation.id)).where(Conversation.project_id == project.id)
    )
    return ProjectResponse(
        id=project.id,
        name=project.name,
        description=project.description or "",
        instructions=project.instructions or "",
        created_at=project.created_at,
        updated_at=project.updated_at,
        file_count=int(fcount or 0),
        conversation_count=int(ccount or 0),
    )


@router.post("", response_model=ProjectResponse)
async def create_project(
    request: ProjectCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    name = request.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="프로젝트 이름을 입력해주세요")
    project = Project(
        user_id=current_user.id,
        name=name[:120],
        description=request.description.strip()[:500],
        instructions=request.instructions,
    )
    db.add(project)
    await db.commit()
    await db.refresh(project)
    return await _to_response(project, db)


@router.get("", response_model=list[ProjectResponse])
async def list_projects(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Project)
        .where(Project.user_id == current_user.id)
        .order_by(Project.updated_at.desc())
    )
    return [await _to_response(p, db) for p in result.scalars().all()]


@router.get("/{project_id}", response_model=ProjectResponse)
async def get_project(
    project_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    project = await _get_own_project(project_id, current_user, db)
    return await _to_response(project, db)


@router.patch("/{project_id}", response_model=ProjectResponse)
async def update_project(
    project_id: int,
    request: ProjectUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    project = await _get_own_project(project_id, current_user, db)
    if request.name is not None:
        name = request.name.strip()
        if not name:
            raise HTTPException(status_code=400, detail="프로젝트 이름을 입력해주세요")
        project.name = name[:120]
    if request.description is not None:
        project.description = request.description.strip()[:500]
    if request.instructions is not None:
        project.instructions = request.instructions
    await db.commit()
    await db.refresh(project)
    return await _to_response(project, db)


@router.delete("/{project_id}")
async def delete_project(
    project_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """프로젝트 삭제. 소속 대화는 삭제하지 않고 일반 대화로 남긴다."""
    project = await _get_own_project(project_id, current_user, db)

    result = await db.execute(
        select(Attachment.stored_name).where(Attachment.project_id == project_id)
    )
    stored_names = [row[0] for row in result.all()]

    await db.delete(project)  # 지식 파일 행은 cascade, 대화의 project_id는 SET NULL
    await db.commit()

    for name in stored_names:
        path = stored_path(name)
        if os.path.isfile(path):
            try:
                os.remove(path)
            except OSError:
                pass
    return {"message": "프로젝트가 삭제되었습니다"}


# ---------- 지식 파일 ----------

_MAX_SKILL_FILES = 80
_MAX_SKILL_UNCOMPRESSED = 8 * 1024 * 1024   # 압축 해제 총량 상한(zip bomb 방지)


def _parse_skill_frontmatter(md: str):
    """SKILL.md 프론트매터(--- name/description ---)와 본문 분리."""
    name = desc = ""
    body = md
    m = re.match(r"^﻿?---\s*\n(.*?)\n---\s*\n?(.*)$", md, re.S)
    if m:
        fm, body = m.group(1), m.group(2)
        for line in fm.splitlines():
            mm = re.match(r"\s*(name|description)\s*:\s*(.*)$", line)
            if mm:
                val = mm.group(2).strip().strip("'\"")
                if mm.group(1) == "name":
                    name = val
                else:
                    desc = val
    return name, desc, body.strip()


@router.post("/import-skill", response_model=ProjectResponse)
async def import_skill(
    request: Request,
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """에이전트 스킬(.skill = SKILL.md + references 묶음 zip)을 프로젝트로 가져온다.
    SKILL.md 본문 → 프로젝트 지침(시스템 프롬프트), references/*.md → 지식 파일."""
    data = await read_upload_or_413(request, file)
    if not data:
        raise HTTPException(status_code=400, detail="빈 파일은 가져올 수 없습니다")
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise HTTPException(status_code=400, detail=".skill(zip) 형식이 아닙니다")

    infos = [i for i in zf.infolist() if not i.is_dir()]
    if not infos:
        raise HTTPException(status_code=400, detail="빈 스킬입니다")
    if len(infos) > _MAX_SKILL_FILES or sum(i.file_size for i in infos) > _MAX_SKILL_UNCOMPRESSED:
        raise HTTPException(status_code=400, detail="스킬 파일이 너무 크거나 많습니다")

    skill_info = next((i for i in infos if i.filename.split("/")[-1].lower() == "skill.md"), None)
    if not skill_info:
        raise HTTPException(status_code=400, detail="SKILL.md가 없습니다(.skill 형식을 확인하세요)")

    def _text(info) -> str:
        try:
            return zf.read(info).decode("utf-8", "replace")
        except Exception:
            return ""

    name, desc, body = _parse_skill_frontmatter(_text(skill_info))
    folder = skill_info.filename.split("/")[0] if "/" in skill_info.filename else ""
    name = (name or folder or "가져온 스킬").strip()[:120]

    project = Project(user_id=current_user.id, name=name,
                      description=(desc or "에이전트 스킬에서 가져옴")[:500],
                      instructions=(body or _text(skill_info)))
    db.add(project)
    await db.flush()

    # 참고 파일(references/*.md 등 텍스트)을 지식 파일로 첨부
    for info in infos:
        if info is skill_info:
            continue
        base = os.path.basename(info.filename)[:255]
        if not base.lower().endswith((".md", ".markdown", ".txt")):
            continue
        raw = zf.read(info)
        txt = raw.decode("utf-8", "replace")
        if not txt.strip():
            continue
        stored = new_stored_name(base)
        with open(stored_path(stored), "wb") as f:
            f.write(raw)
        db.add(Attachment(user_id=current_user.id, project_id=project.id, filename=base,
                          content_type="text/markdown", size_bytes=len(raw), stored_name=stored,
                          text_content=txt[:200_000]))
    await db.commit()
    await db.refresh(project)
    return await _to_response(project, db)


@router.post("/{project_id}/files", response_model=AttachmentResponse)
async def upload_project_file(
    project_id: int,
    request: Request,
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    project = await _get_own_project(project_id, current_user, db)

    data = await read_upload_or_413(request, file)
    if not data:
        raise HTTPException(status_code=400, detail="빈 파일은 업로드할 수 없습니다")

    filename = os.path.basename(file.filename or "파일")[:255]
    content_type = (file.content_type or "application/octet-stream")[:120]
    text = extract_text(data, filename, content_type)

    stored = new_stored_name(filename)
    with open(stored_path(stored), "wb") as f:
        f.write(data)

    att = Attachment(
        user_id=current_user.id,
        project_id=project.id,
        filename=filename,
        content_type=content_type,
        size_bytes=len(data),
        stored_name=stored,
        text_content=text,
    )
    db.add(att)
    await db.commit()
    await db.refresh(att)
    return attachment_response(att)


@router.get("/{project_id}/files", response_model=list[AttachmentResponse])
async def list_project_files(
    project_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await _get_own_project(project_id, current_user, db)
    result = await db.execute(
        select(Attachment)
        .where(Attachment.project_id == project_id)
        .order_by(Attachment.id.asc())
    )
    return [attachment_response(a) for a in result.scalars().all()]


@router.delete("/{project_id}/files/{file_id}")
async def delete_project_file(
    project_id: int,
    file_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await _get_own_project(project_id, current_user, db)
    result = await db.execute(
        select(Attachment).where(Attachment.id == file_id, Attachment.project_id == project_id)
    )
    att = result.scalar_one_or_none()
    if not att:
        raise HTTPException(status_code=404, detail="파일을 찾을 수 없습니다")
    path = stored_path(att.stored_name)
    await db.delete(att)
    await db.commit()
    if os.path.isfile(path):
        try:
            os.remove(path)
        except OSError:
            pass
    return {"message": "삭제되었습니다"}
