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
    # v3: 역할 확장(담당자) + 멀티 프로바이더
    "ALTER TYPE userrole ADD VALUE IF NOT EXISTS 'manager'",
    'ALTER TABLE api_keys ADD COLUMN IF NOT EXISTS extra TEXT',
    # 보안: 비밀번호 변경 시 기존 토큰 무효화용 버전
    'ALTER TABLE users ADD COLUMN IF NOT EXISTS token_version INTEGER NOT NULL DEFAULT 0',
    # v4: 문서/이미지 생성
    "ALTER TABLE attachments ADD COLUMN IF NOT EXISTS kind VARCHAR(20) NOT NULL DEFAULT 'upload'",
    'ALTER TABLE attachments ADD COLUMN IF NOT EXISTS origin VARCHAR(80)',
    "ALTER TABLE model_routes ADD COLUMN IF NOT EXISTS kind VARCHAR(20) NOT NULL DEFAULT 'chat'",
]


# 기본 모델 라우팅 시드: (key, label, provider, provider_model_id, description, sort)
DEFAULT_MODEL_ROUTES = [
    ("sonnet",      "Claude Sonnet 4.6", "anthropic", "claude-sonnet-4-6",          "균형잡힌 성능 · 추천", 10),
    ("opus",        "Claude Opus 4.6",   "anthropic", "claude-opus-4-6",            "최고 성능", 11),
    ("haiku",       "Claude Haiku 4.5",  "anthropic", "claude-haiku-4-5-20251001",  "빠름 · 저렴", 12),
    ("gpt-5-pro",   "GPT-5 Pro",         "gabia",     "gpt-5.4-pro",                "OpenAI 최고 성능", 20),
    ("gpt-5",       "GPT-5",             "gabia",     "gpt-5.2",                    "OpenAI 균형", 21),
    ("o4-mini",     "o4-mini",           "gabia",     "o4-mini",                    "빠른 추론", 22),
    ("codex",       "GPT Codex",         "gabia",     "gpt-5.3-codex",              "코딩 특화", 23),
    ("deepseek",    "DeepSeek R1",       "gabia",     "deepseek-r1-0528",           "오픈소스 추론", 30),
    ("gemini",      "Gemini Flash",      "gabia",     "gemini-3.1-flash-lite-preview", "Google 빠름", 31),
    ("qwen",        "Qwen3.5 122B",      "gabia",     "qwen3.5-122b-a10b",          "Alibaba 대형", 32),
    ("qwen-plus",   "Qwen3.6 Plus",      "gabia",     "qwen3.6-plus",               "Alibaba 최신", 33),
    ("llama",       "Llama 3.2 Vision",  "gabia",     "llama-3.2-11b-vision",       "Meta 비전", 34),
    ("kimi",        "Kimi K2",           "gabia",     "kimi-k2-instruct",           "Moonshot 최신", 35),
    ("kimi-think",  "Kimi K2 Think",     "gabia",     "kimi-k2-thinking",           "추론 특화", 36),
    ("minimax",     "MiniMax M2",        "gabia",     "minimax-m2.1",               "MiniMax 최신", 37),
    ("sonar",       "Sonar Pro",         "gabia",     "sonar-pro-search",           "웹 검색 포함", 38),
    ("sonar-deep",  "Sonar Research",    "gabia",     "sonar-deep-research",        "심층 리서치", 39),
    ("glm",         "GLM-4.7",           "gabia",     "glm-4.7",                    "ZAI 최신", 40),
    ("mimo",        "MiMo Pro",          "gabia",     "mimo-v2.5-pro",              "Xiaomi AI", 41),
]


async def seed_model_routes():
    """model_routes 테이블이 비어 있으면 기본 라우팅을 채운다."""
    from sqlalchemy import select, func
    from app.models import ModelRoute

    async with AsyncSessionLocal() as db:
        count = await db.scalar(select(func.count(ModelRoute.id)))
        if count:
            return
        for key, label, provider, model_id, desc, sort in DEFAULT_MODEL_ROUTES:
            db.add(ModelRoute(
                key=key, label=label, provider=provider,
                provider_model_id=model_id, description=desc, sort=sort,
            ))
        await db.commit()
        print(f"[DB] 기본 모델 라우팅 {len(DEFAULT_MODEL_ROUTES)}개 시드 완료")


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

    # 테이블 생성
    async with engine.begin() as conn:
        from app import models  # noqa: F401
        await conn.run_sync(Base.metadata.create_all)

    # 컬럼/타입 마이그레이션 (ALTER TYPE은 트랜잭션 제약이 있어 AUTOCOMMIT으로 실행)
    async with engine.connect() as conn:
        auto = await conn.execution_options(isolation_level="AUTOCOMMIT")
        for stmt in MIGRATION_STATEMENTS:
            await auto.execute(text(stmt))

    await seed_model_routes()
    print("[DB] 테이블 초기화 완료")
