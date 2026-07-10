"""HR 연동 관리 API(최고관리자 전용)."""
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.dependencies import get_current_user, get_superadmin_user
from app.models import User
from app import hr_sync

router = APIRouter(prefix="/admin/hr", tags=["hr"])
# 개인용(본인 전용) 라우터 — 관리자 권한과 분리. 본인 사번으로만, 재검증 후 제공.
me_router = APIRouter(prefix="/hr", tags=["hr"])


@me_router.get("/me")
async def hr_me(current_user: User = Depends(get_current_user)):
    """로그인한 **본인**의 HR 인사정보만 반환. 클라이언트는 사번을 지정할 수 없으며,
    서버가 보유한 본인 사번으로만 조회하고 응답을 재검증한다. 확인 불가 시 403."""
    if not hr_sync.hr_configured():
        raise HTTPException(status_code=503, detail="HR 연동이 설정되지 않았습니다")
    try:
        rec = await hr_sync.fetch_own_employee(current_user)
    except hr_sync.HRSyncError as e:
        raise HTTPException(status_code=502, detail=str(e))
    if rec is None:
        raise HTTPException(status_code=403,
                            detail="본인 확인이 되지 않아 인사정보를 제공할 수 없습니다.")
    g = hr_sync._g
    # 필요한 최소 항목만 노출(전체 응답을 그대로 흘리지 않음)
    return {
        "employee_no": current_user.employee_no,
        "name": g(rec, "name", "성명", default=current_user.name or ""),
        "department": g(rec, "department_name", "department", "부서", default=""),
        "position": g(rec, "position", "job_grade", "직위", "직급", default=""),
        "status": g(rec, "status", "재직상태", default=""),
        "hired_at": g(rec, "hired_at", "입사일", default=""),
    }


@router.get("/config")
async def hr_config(_: User = Depends(get_superadmin_user)):
    """연동 설정 상태(키 노출 없이). 프론트에서 버튼 활성/안내용."""
    host = ""
    if settings.hr_base_url:
        try:
            host = urlparse(settings.hr_base_url).netloc or settings.hr_base_url
        except ValueError:
            host = settings.hr_base_url
    return {"configured": hr_sync.hr_configured(), "host": host,
            "auto_create": bool(settings.hr_auto_create)}


@router.post("/sync")
async def hr_run_sync(updated_since: str | None = None, auto_create: bool | None = None,
                      _: User = Depends(get_superadmin_user),
                      db: AsyncSession = Depends(get_db)):
    """HR 명부로 직원 계정 동기화 실행. 결과 요약(생성/갱신/비활성/오류) 반환.
    생성된 계정의 임시 비밀번호는 이 응답에서 한 번만 확인 가능."""
    if not hr_sync.hr_configured():
        raise HTTPException(status_code=400,
                            detail="HR 연동이 설정되지 않았습니다 (.env HR_BASE_URL, HR_API_KEY).")
    try:
        return await hr_sync.sync_employees(db, updated_since=updated_since, auto_create=auto_create)
    except hr_sync.HRSyncError as e:
        raise HTTPException(status_code=502, detail=str(e))
