from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from passlib.context import CryptContext
from sqlalchemy import select

from app.config import settings
from app.database import AsyncSessionLocal, init_db
from app.middleware import IPWhitelistMiddleware
from app.models import User, UserRole
from app.routers import auth, chat, users, keys

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()

    # superadmin 계정 초기 생성
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(User).where(User.username == settings.superadmin_username))
        if not result.scalar_one_or_none():
            superadmin = User(
                username=settings.superadmin_username,
                hashed_password=pwd_context.hash(settings.superadmin_initial_password),
                role=UserRole.superadmin,
                force_password_reset=True,
            )
            db.add(superadmin)
            await db.commit()
            print(f"[INIT] superadmin 계정 생성 완료 (초기 비밀번호: {settings.superadmin_initial_password})")

    yield


app = FastAPI(title="HP Engineering AI Hub", lifespan=lifespan)

app.add_middleware(IPWhitelistMiddleware, allowed_ips=settings.allowed_ips)

app.include_router(auth.router, prefix="/api")
app.include_router(chat.router, prefix="/api")
app.include_router(users.router, prefix="/api")
app.include_router(keys.router, prefix="/api")

app.mount("/static", StaticFiles(directory="app/static"), name="static")


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/{full_path:path}")
async def serve_frontend(full_path: str):
    return FileResponse("app/static/index.html")
