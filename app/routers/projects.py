import os
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
