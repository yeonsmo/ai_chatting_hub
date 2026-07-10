from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from passlib.context import CryptContext
from sqlalchemy import select

from app.config import settings
from app.database import AsyncSessionLocal, init_db
from app.file_utils import ensure_upload_dir
from app.middleware import IPWhitelistMiddleware, MaxBodySizeMiddleware, SecurityHeadersMiddleware
from app.models import User, UserRole
import asyncio
from app.routers import (auth, chat, users, keys, files, projects, admin,
                         settings_admin, minutes, reference, feedback, hr, documents)

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    ensure_upload_dir()

    # superadmin 계정 초기 생성
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(User).where(User.username == settings.superadmin_username))
        if not result.scalar_one_or_none():
            if not settings.superadmin_initial_password:
                print("[INIT][경고] SUPERADMIN_INITIAL_PASSWORD가 비어 있어 superadmin 계정을 만들지 않았습니다.")
            else:
                superadmin = User(
                    username=settings.superadmin_username,
                    hashed_password=pwd_context.hash(settings.superadmin_initial_password),
                    role=UserRole.superadmin,
                    force_password_reset=True,
                )
                db.add(superadmin)
                await db.commit()
                print("[INIT] superadmin 계정 생성 완료 (초기 비밀번호는 .env에 설정한 값)")

        # 사내 자료실 기본자료 시드(파일명 기준 중복 방지)
        try:
            await reference.seed_reference_docs(db)
        except Exception as e:  # noqa: BLE001 — 시드 실패가 기동을 막지 않도록
            print(f"[INIT][경고] 자료실 시드 실패: {e}")

    # 보류 서류 자동삭제(1일 경과) 주기 정리 태스크
    async def _purge_loop():
        while True:
            try:
                async with AsyncSessionLocal() as db:
                    n = await documents.purge_expired_held(db)
                    if n:
                        print(f"[CLEANUP] 만료된 보류 서류 {n}건 자동삭제")
            except Exception as e:  # noqa: BLE001
                print(f"[CLEANUP][경고] 보류 서류 정리 실패: {e}")
            await asyncio.sleep(3600)   # 1시간마다

    purge_task = asyncio.create_task(_purge_loop())
    try:
        yield
    finally:
        purge_task.cancel()


app = FastAPI(title="Gabia AI 허브", lifespan=lifespan)

app.add_middleware(
    IPWhitelistMiddleware,
    allowed_ips=settings.allowed_ips,
    trusted_proxies=settings.trusted_proxies,
)

# 전역 요청 본문 상한(업로드 최대치 + 멀티파트 오버헤드 여유). 마지막에 추가해 최외곽에서
# 먼저 동작하도록 한다 → 과대 요청을 인증/파싱 전에 차단.
app.add_middleware(
    MaxBodySizeMiddleware,
    max_body_bytes=settings.max_upload_mb * 1024 * 1024 + 1024 * 1024,
)

# 모든 응답에 보안 헤더(CSP 등) 부여
app.add_middleware(SecurityHeadersMiddleware)

app.include_router(auth.router, prefix="/api")
app.include_router(chat.router, prefix="/api")
app.include_router(users.router, prefix="/api")
app.include_router(keys.router, prefix="/api")
app.include_router(files.router, prefix="/api")
app.include_router(minutes.router, prefix="/api")
app.include_router(projects.router, prefix="/api")
app.include_router(admin.router, prefix="/api")
app.include_router(settings_admin.router, prefix="/api")
app.include_router(reference.router, prefix="/api")
app.include_router(feedback.router, prefix="/api")
app.include_router(hr.router, prefix="/api")
app.include_router(hr.me_router, prefix="/api")
app.include_router(documents.router, prefix="/api")

app.mount("/static", StaticFiles(directory="app/static"), name="static")


@app.get("/health")
async def health():
    return {"status": "ok"}


# PWA: 서비스워커는 루트 스코프에서 서빙해야 전체 앱을 제어할 수 있다
@app.get("/sw.js")
async def service_worker():
    return FileResponse("app/static/sw.js", media_type="application/javascript")


@app.get("/manifest.webmanifest")
async def manifest():
    return FileResponse("app/static/manifest.webmanifest", media_type="application/manifest+json")


@app.get("/{full_path:path}")
async def serve_frontend(full_path: str):
    # 존재하지 않는 API 경로가 HTML로 응답되지 않도록 차단
    if full_path == "api" or full_path.startswith("api/"):
        raise HTTPException(status_code=404, detail="Not Found")
    return FileResponse("app/static/index.html")
