from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from passlib.context import CryptContext
from sqlalchemy import select

from app.config import settings
from app.database import AsyncSessionLocal, init_db
from app.file_utils import ensure_upload_dir
from app.middleware import IPWhitelistMiddleware
from app.models import User, UserRole
from app.routers import auth, chat, users, keys, files, projects, admin

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

    yield


app = FastAPI(title="Gabia AI 허브", lifespan=lifespan)

app.add_middleware(
    IPWhitelistMiddleware,
    allowed_ips=settings.allowed_ips,
    trusted_proxies=settings.trusted_proxies,
)

app.include_router(auth.router, prefix="/api")
app.include_router(chat.router, prefix="/api")
app.include_router(users.router, prefix="/api")
app.include_router(keys.router, prefix="/api")
app.include_router(files.router, prefix="/api")
app.include_router(projects.router, prefix="/api")
app.include_router(admin.router, prefix="/api")

app.mount("/static", StaticFiles(directory="app/static"), name="static")


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/{full_path:path}")
async def serve_frontend(full_path: str):
    # 존재하지 않는 API 경로가 HTML로 응답되지 않도록 차단
    if full_path == "api" or full_path.startswith("api/"):
        raise HTTPException(status_code=404, detail="Not Found")
    return FileResponse("app/static/index.html")
