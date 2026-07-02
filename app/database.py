import asyncpg
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker, declarative_base
from sqlalchemy import text
from app.config import settings

Base = declarative_base()

engine = create_async_engine(settings.database_url, echo=False)
AsyncSessionLocal = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def get_db():
    async with AsyncSessionLocal() as session:
        yield session


# 기존 배포(v1) 테이블에 새 컬럼을 더하는 경량 마이그레이션
MIGRATION_STATEMENTS = [
    'ALTER TABLE conversations ADD COLUMN IF NOT EXISTS project_id INTEGER REFERENCES projects(id) ON DELETE SET NULL',
    'ALTER TABLE conversations ADD COLUMN IF NOT EXISTS is_archived BOOLEAN NOT NULL DEFAULT FALSE',
    'ALTER TABLE messages ADD COLUMN IF NOT EXISTS model VARCHAR(80)',
    'ALTER TABLE messages ADD COLUMN IF NOT EXISTS provider VARCHAR(40)',
]


async def init_db():
    # postgres 기본 DB에 접속해서 claude_chat DB 생성
    conn = await asyncpg.connect(
        host=settings.db_host,
        port=settings.db_port,
        user=settings.db_user,
        password=settings.db_password,
        database="postgres"
    )
    try:
        exists = await conn.fetchval(
            "SELECT 1 FROM pg_database WHERE datname = $1", settings.db_name
        )
        if not exists:
            await conn.execute(f'CREATE DATABASE "{settings.db_name}"')
            print(f"[DB] {settings.db_name} 데이터베이스 생성 완료")
    finally:
        await conn.close()

    # 테이블 생성 + 컬럼 마이그레이션
    async with engine.begin() as conn:
        from app import models  # noqa: F401
        await conn.run_sync(Base.metadata.create_all)
        for stmt in MIGRATION_STATEMENTS:
            await conn.execute(text(stmt))
    print("[DB] 테이블 초기화 완료")
