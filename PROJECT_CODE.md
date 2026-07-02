# HP Engineering AI Hub - 전체 코드

## 프로젝트 구조

```
claude-chat/
├── docker-compose.yml
├── Dockerfile
├── requirements.txt
├── .env
└── app/
    ├── __init__.py
    ├── main.py
    ├── config.py
    ├── database.py
    ├── models.py
    ├── schemas.py
    ├── middleware.py
    ├── dependencies.py
    ├── routers/
    │   ├── __init__.py
    │   ├── auth.py
    │   ├── chat.py
    │   ├── users.py
    │   └── keys.py
    └── static/
        └── index.html
```

---

## docker-compose.yml

```yaml
services:
  claude-chat:
    build: .
    container_name: claude-chat
    restart: always
    ports:
      - "3899:3899"
    env_file:
      - .env
    networks:
      - n8n_default

networks:
  n8n_default:
    external: true
```

---

## Dockerfile

```dockerfile
FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "3899"]
```

---

## requirements.txt

```
fastapi==0.115.0
uvicorn[standard]==0.30.6
sqlalchemy[asyncio]==2.0.35
asyncpg==0.29.0
passlib[bcrypt]==1.7.4
bcrypt==4.0.1
python-jose[cryptography]==3.3.0
anthropic==0.34.2
pydantic-settings==2.5.2
python-multipart==0.0.12
```

---

## .env

```
# Database (n8n과 공유 PostgreSQL)
DB_HOST=postgres
DB_PORT=5432
DB_USER=n8n
DB_PASSWORD=your_db_password
DB_NAME=claude_chat

# JWT 보안 키 (반드시 변경하세요)
SECRET_KEY=change_me_to_a_random_secret
ALGORITHM=HS256
ACCESS_TOKEN_EXPIRE_MINUTES=480

# IP 화이트리스트 (쉼표로 구분, CIDR 표기 가능)
ALLOWED_IPS=192.168.219.0/24,127.0.0.1

# Claude API 키 (관리자 패널에서도 추가 가능)
ANTHROPIC_API_KEY=

# Superadmin 설정
SUPERADMIN_USERNAME=superadmin
SUPERADMIN_INITIAL_PASSWORD=change_me
```

---

## app/config.py

```python
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    db_host: str = "postgres"
    db_port: int = 5432
    db_user: str = "n8n"
    db_password: str = ""
    db_name: str = "claude_chat"

    secret_key: str = ""
    algorithm: str = "HS256"
    access_token_expire_minutes: int = 480

    allowed_ips: str = "192.168.219.0/24,127.0.0.1"

    anthropic_api_key: str = ""

    superadmin_username: str = "superadmin"
    superadmin_initial_password: str = ""

    @property
    def database_url(self) -> str:
        return f"postgresql+asyncpg://{self.db_user}:{self.db_password}@{self.db_host}:{self.db_port}/{self.db_name}"

    class Config:
        env_file = ".env"


settings = Settings()
```

---

## app/database.py

```python
import asyncpg
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker, declarative_base
from app.config import settings

Base = declarative_base()

engine = create_async_engine(settings.database_url, echo=False)
AsyncSessionLocal = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def get_db():
    async with AsyncSessionLocal() as session:
        yield session


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
    print("[DB] 테이블 초기화 완료")
```

---

## app/models.py

```python
import enum
from datetime import datetime
from sqlalchemy import Column, Integer, String, Boolean, DateTime, Text, ForeignKey, Enum
from sqlalchemy.orm import relationship
from app.database import Base


class UserRole(str, enum.Enum):
    superadmin = "superadmin"
    admin = "admin"
    user = "user"


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    username = Column(String(50), unique=True, index=True, nullable=False)
    email = Column(String(100), unique=True, nullable=True)
    hashed_password = Column(String(255), nullable=False)
    role = Column(Enum(UserRole), default=UserRole.user, nullable=False)
    is_active = Column(Boolean, default=True)
    force_password_reset = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    created_by = Column(Integer, ForeignKey("users.id"), nullable=True)

    conversations = relationship("Conversation", back_populates="user", foreign_keys="Conversation.user_id")


class Conversation(Base):
    __tablename__ = "conversations"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    title = Column(String(255), default="새 대화")
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    user = relationship("User", back_populates="conversations", foreign_keys=[user_id])
    messages = relationship("Message", back_populates="conversation", cascade="all, delete-orphan")


class Message(Base):
    __tablename__ = "messages"

    id = Column(Integer, primary_key=True, index=True)
    conversation_id = Column(Integer, ForeignKey("conversations.id"), nullable=False)
    role = Column(String(20), nullable=False)  # "user" or "assistant"
    content = Column(Text, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)

    conversation = relationship("Conversation", back_populates="messages")


class APIKey(Base):
    __tablename__ = "api_keys"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(100), nullable=False)
    provider = Column(String(50), nullable=False)  # anthropic, openai, gemini, etc.
    key_value = Column(String(500), nullable=False)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    created_by = Column(Integer, ForeignKey("users.id"), nullable=False)
```

---

## app/schemas.py

```python
from pydantic import BaseModel
from typing import Optional
from datetime import datetime
from app.models import UserRole


class LoginRequest(BaseModel):
    username: str
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    force_password_reset: bool = False


class PasswordChangeRequest(BaseModel):
    current_password: str
    new_password: str


class UserCreate(BaseModel):
    username: str
    email: Optional[str] = None
    password: str
    role: UserRole = UserRole.user


class UserResponse(BaseModel):
    id: int
    username: str
    email: Optional[str]
    role: UserRole
    is_active: bool
    force_password_reset: bool
    created_at: datetime

    class Config:
        from_attributes = True


class MessageCreate(BaseModel):
    content: str
    conversation_id: Optional[int] = None


class MessageResponse(BaseModel):
    id: int
    role: str
    content: str
    created_at: datetime

    class Config:
        from_attributes = True


class ConversationResponse(BaseModel):
    id: int
    title: str
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class ChatResponse(BaseModel):
    conversation_id: int
    message: MessageResponse


class APIKeyCreate(BaseModel):
    name: str
    provider: str
    key_value: str


class APIKeyResponse(BaseModel):
    id: int
    name: str
    provider: str
    key_value: str
    is_active: bool
    created_at: datetime

    class Config:
        from_attributes = True
```

---

## app/middleware.py

```python
import ipaddress
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response


class IPWhitelistMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, allowed_ips: str):
        super().__init__(app)
        self.allowed_networks = []
        for ip_str in allowed_ips.split(","):
            ip_str = ip_str.strip()
            try:
                self.allowed_networks.append(ipaddress.ip_network(ip_str, strict=False))
            except ValueError:
                try:
                    self.allowed_networks.append(ipaddress.ip_address(ip_str))
                except ValueError:
                    pass

    def is_allowed(self, ip: str) -> bool:
        try:
            client_ip = ipaddress.ip_address(ip)
            for network in self.allowed_networks:
                if isinstance(network, (ipaddress.IPv4Network, ipaddress.IPv6Network)):
                    if client_ip in network:
                        return True
                else:
                    if client_ip == network:
                        return True
        except ValueError:
            pass
        return False

    async def dispatch(self, request, call_next):
        if request.url.path == "/health":
            return await call_next(request)

        # X-Forwarded-For 우선 (Nginx 리버스 프록시 환경)
        client_ip = request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
        if not client_ip:
            client_ip = request.client.host if request.client else ""

        if not self.is_allowed(client_ip):
            return Response(content="Access denied", status_code=403)

        return await call_next(request)
```

---

## app/dependencies.py

```python
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from jose import JWTError, jwt
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from app.config import settings
from app.database import get_db
from app.models import User, UserRole

security = HTTPBearer()


async def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(security),
    db: AsyncSession = Depends(get_db),
) -> User:
    exc = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="인증 정보가 유효하지 않습니다",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = jwt.decode(credentials.credentials, settings.secret_key, algorithms=[settings.algorithm])
        username: str = payload.get("sub")
        if not username:
            raise exc
    except JWTError:
        raise exc

    result = await db.execute(select(User).where(User.username == username))
    user = result.scalar_one_or_none()

    if not user or not user.is_active:
        raise exc
    return user


async def get_admin_user(current_user: User = Depends(get_current_user)) -> User:
    if current_user.role not in [UserRole.admin, UserRole.superadmin]:
        raise HTTPException(status_code=403, detail="관리자 권한이 필요합니다")
    return current_user


async def get_superadmin_user(current_user: User = Depends(get_current_user)) -> User:
    if current_user.role != UserRole.superadmin:
        raise HTTPException(status_code=403, detail="최고 관리자 권한이 필요합니다")
    return current_user
```

---

## app/main.py

```python
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
```

---

## app/routers/auth.py

```python
from datetime import datetime, timedelta
from fastapi import APIRouter, Depends, HTTPException
from jose import jwt
from passlib.context import CryptContext
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from app.config import settings
from app.database import get_db
from app.dependencies import get_current_user
from app.models import User
from app.schemas import LoginRequest, TokenResponse, PasswordChangeRequest

router = APIRouter(prefix="/auth", tags=["auth"])
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


def create_access_token(username: str) -> str:
    expire = datetime.utcnow() + timedelta(minutes=settings.access_token_expire_minutes)
    return jwt.encode({"sub": username, "exp": expire}, settings.secret_key, algorithm=settings.algorithm)


@router.post("/login", response_model=TokenResponse)
async def login(request: LoginRequest, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(User).where(User.username == request.username))
    user = result.scalar_one_or_none()

    if not user or not pwd_context.verify(request.password, user.hashed_password):
        raise HTTPException(status_code=401, detail="아이디 또는 비밀번호가 틀렸습니다")
    if not user.is_active:
        raise HTTPException(status_code=403, detail="비활성화된 계정입니다")

    return TokenResponse(
        access_token=create_access_token(user.username),
        force_password_reset=user.force_password_reset,
    )


@router.post("/change-password")
async def change_password(
    request: PasswordChangeRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if not pwd_context.verify(request.current_password, current_user.hashed_password):
        raise HTTPException(status_code=400, detail="현재 비밀번호가 틀렸습니다")
    if len(request.new_password) < 8:
        raise HTTPException(status_code=400, detail="비밀번호는 8자 이상이어야 합니다")

    current_user.hashed_password = pwd_context.hash(request.new_password)
    current_user.force_password_reset = False
    await db.commit()
    return {"message": "비밀번호가 변경되었습니다"}


@router.get("/me")
async def get_me(current_user: User = Depends(get_current_user)):
    return {
        "id": current_user.id,
        "username": current_user.username,
        "role": current_user.role,
        "force_password_reset": current_user.force_password_reset,
    }
```

---

## app/routers/chat.py

```python
from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
import anthropic
from app.config import settings
from app.database import get_db
from app.dependencies import get_current_user
from app.models import User, Conversation, Message, APIKey
from app.schemas import MessageCreate, ChatResponse, ConversationResponse, MessageResponse

router = APIRouter(prefix="/chat", tags=["chat"])


async def get_anthropic_key(db: AsyncSession) -> str:
    result = await db.execute(
        select(APIKey).where(APIKey.provider == "anthropic", APIKey.is_active == True)
    )
    key_obj = result.scalar_one_or_none()
    key = key_obj.key_value if key_obj else settings.anthropic_api_key
    if not key:
        raise HTTPException(status_code=500, detail="Anthropic API 키가 설정되지 않았습니다. 관리자에게 문의하세요.")
    return key


@router.post("/send", response_model=ChatResponse)
async def send_message(
    request: MessageCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    # 대화 조회 또는 생성
    if request.conversation_id:
        result = await db.execute(
            select(Conversation).where(
                Conversation.id == request.conversation_id,
                Conversation.user_id == current_user.id,
            )
        )
        conversation = result.scalar_one_or_none()
        if not conversation:
            raise HTTPException(status_code=404, detail="대화를 찾을 수 없습니다")
    else:
        conversation = Conversation(user_id=current_user.id)
        db.add(conversation)
        await db.flush()

    # 사용자 메시지 저장
    user_msg = Message(conversation_id=conversation.id, role="user", content=request.content)
    db.add(user_msg)
    await db.flush()

    # 대화 히스토리 조회 (최근 50개)
    history_result = await db.execute(
        select(Message)
        .where(Message.conversation_id == conversation.id)
        .order_by(Message.created_at.asc())
        .limit(50)
    )
    messages = history_result.scalars().all()

    claude_messages = [{"role": m.role, "content": m.content} for m in messages]

    # Claude API 호출
    api_key = await get_anthropic_key(db)
    client = anthropic.AsyncAnthropic(api_key=api_key)
    response = await client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=4096,
        messages=claude_messages,
    )

    assistant_content = response.content[0].text

    # 응답 저장
    assistant_msg = Message(
        conversation_id=conversation.id,
        role="assistant",
        content=assistant_content,
    )
    db.add(assistant_msg)

    # 첫 메시지이면 제목 설정
    if len(messages) == 1:
        conversation.title = request.content[:60] + ("..." if len(request.content) > 60 else "")

    conversation.updated_at = datetime.utcnow()
    await db.commit()
    await db.refresh(assistant_msg)

    return ChatResponse(
        conversation_id=conversation.id,
        message=MessageResponse(
            id=assistant_msg.id,
            role=assistant_msg.role,
            content=assistant_msg.content,
            created_at=assistant_msg.created_at,
        ),
    )


@router.get("/conversations", response_model=list[ConversationResponse])
async def get_conversations(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Conversation)
        .where(Conversation.user_id == current_user.id)
        .order_by(Conversation.updated_at.desc())
    )
    return result.scalars().all()


@router.get("/conversations/{conversation_id}/messages", response_model=list[MessageResponse])
async def get_messages(
    conversation_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Conversation).where(
            Conversation.id == conversation_id,
            Conversation.user_id == current_user.id,
        )
    )
    if not result.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="대화를 찾을 수 없습니다")

    result = await db.execute(
        select(Message)
        .where(Message.conversation_id == conversation_id)
        .order_by(Message.created_at.asc())
    )
    return result.scalars().all()


@router.delete("/conversations/{conversation_id}")
async def delete_conversation(
    conversation_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Conversation).where(
            Conversation.id == conversation_id,
            Conversation.user_id == current_user.id,
        )
    )
    conversation = result.scalar_one_or_none()
    if not conversation:
        raise HTTPException(status_code=404, detail="대화를 찾을 수 없습니다")

    await db.delete(conversation)
    await db.commit()
    return {"message": "대화가 삭제되었습니다"}
```

---

## app/routers/users.py

```python
from fastapi import APIRouter, Depends, HTTPException
from passlib.context import CryptContext
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from app.database import get_db
from app.dependencies import get_admin_user, get_superadmin_user
from app.models import User, UserRole
from app.schemas import UserCreate, UserResponse

router = APIRouter(prefix="/users", tags=["users"])
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


@router.post("", response_model=UserResponse)
async def create_user(
    request: UserCreate,
    current_user: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db),
):
    if request.role == UserRole.superadmin:
        raise HTTPException(status_code=403, detail="최고 관리자 계정은 생성할 수 없습니다")
    if request.role == UserRole.admin and current_user.role != UserRole.superadmin:
        raise HTTPException(status_code=403, detail="관리자 계정은 최고 관리자만 생성할 수 있습니다")

    result = await db.execute(select(User).where(User.username == request.username))
    if result.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="이미 존재하는 사용자명입니다")

    user = User(
        username=request.username,
        email=request.email,
        hashed_password=pwd_context.hash(request.password),
        role=request.role,
        force_password_reset=True,
        created_by=current_user.id,
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


@router.get("", response_model=list[UserResponse])
async def list_users(
    current_user: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db),
):
    if current_user.role == UserRole.superadmin:
        result = await db.execute(select(User))
    else:
        result = await db.execute(select(User).where(User.created_by == current_user.id))
    return result.scalars().all()


@router.patch("/{user_id}/toggle-active")
async def toggle_user_active(
    user_id: int,
    current_user: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()

    if not user:
        raise HTTPException(status_code=404, detail="사용자를 찾을 수 없습니다")
    if user.role == UserRole.superadmin:
        raise HTTPException(status_code=403, detail="최고 관리자 계정은 변경할 수 없습니다")
    if current_user.role == UserRole.admin and user.created_by != current_user.id:
        raise HTTPException(status_code=403, detail="권한이 없습니다")

    user.is_active = not user.is_active
    await db.commit()
    return {"message": f"{'활성화' if user.is_active else '비활성화'} 완료", "is_active": user.is_active}


@router.delete("/{user_id}")
async def delete_user(
    user_id: int,
    current_user: User = Depends(get_superadmin_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()

    if not user or user.role == UserRole.superadmin:
        raise HTTPException(status_code=404, detail="사용자를 찾을 수 없습니다")

    await db.delete(user)
    await db.commit()
    return {"message": "사용자가 삭제되었습니다"}
```

---

## app/routers/keys.py

```python
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from app.database import get_db
from app.dependencies import get_admin_user
from app.models import APIKey, User
from app.schemas import APIKeyCreate, APIKeyResponse

router = APIRouter(prefix="/keys", tags=["api-keys"])


def mask_key(key: str) -> str:
    if len(key) <= 8:
        return "****"
    return key[:4] + "****" + key[-4:]


@router.post("", response_model=APIKeyResponse)
async def create_api_key(
    request: APIKeyCreate,
    current_user: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db),
):
    api_key = APIKey(
        name=request.name,
        provider=request.provider,
        key_value=request.key_value,
        created_by=current_user.id,
    )
    db.add(api_key)
    await db.commit()
    await db.refresh(api_key)
    api_key.key_value = mask_key(api_key.key_value)
    return api_key


@router.get("", response_model=list[APIKeyResponse])
async def list_api_keys(
    current_user: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(APIKey))
    keys = result.scalars().all()
    for key in keys:
        key.key_value = mask_key(key.key_value)
    return keys


@router.patch("/{key_id}/toggle")
async def toggle_api_key(
    key_id: int,
    current_user: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(APIKey).where(APIKey.id == key_id))
    key = result.scalar_one_or_none()
    if not key:
        raise HTTPException(status_code=404, detail="API 키를 찾을 수 없습니다")
    key.is_active = not key.is_active
    await db.commit()
    return {"message": f"{'활성화' if key.is_active else '비활성화'} 완료", "is_active": key.is_active}


@router.delete("/{key_id}")
async def delete_api_key(
    key_id: int,
    current_user: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(APIKey).where(APIKey.id == key_id))
    key = result.scalar_one_or_none()
    if not key:
        raise HTTPException(status_code=404, detail="API 키를 찾을 수 없습니다")
    await db.delete(key)
    await db.commit()
    return {"message": "API 키가 삭제되었습니다"}
```

---

## app/static/index.html

```html
<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>HP Engineering AI Hub</title>
<script src="https://cdn.tailwindcss.com"></script>
<script src="https://cdn.jsdelivr.net/npm/marked/marked.min.js"></script>
<style>
  .msg-content h1{font-size:1.4rem;font-weight:700;margin:.6rem 0}
  .msg-content h2{font-size:1.2rem;font-weight:700;margin:.5rem 0}
  .msg-content h3{font-size:1.05rem;font-weight:600;margin:.4rem 0}
  .msg-content p{margin:.4rem 0;line-height:1.65}
  .msg-content ul{list-style:disc;padding-left:1.4rem;margin:.4rem 0}
  .msg-content ol{list-style:decimal;padding-left:1.4rem;margin:.4rem 0}
  .msg-content li{margin:.2rem 0}
  .msg-content code{background:#1e293b;padding:.1rem .35rem;border-radius:.3rem;font-family:monospace;font-size:.875em}
  .msg-content pre{background:#1e293b;padding:1rem;border-radius:.6rem;overflow-x:auto;margin:.5rem 0}
  .msg-content pre code{background:none;padding:0}
  .msg-content blockquote{border-left:3px solid #3b82f6;padding-left:.9rem;color:#94a3b8;margin:.5rem 0}
  .msg-content table{border-collapse:collapse;width:100%;margin:.5rem 0}
  .msg-content th,.msg-content td{border:1px solid #334155;padding:.4rem .6rem;text-align:left}
  .msg-content th{background:#1e293b}
  .msg-content a{color:#60a5fa;text-decoration:underline}
  ::-webkit-scrollbar{width:5px}
  ::-webkit-scrollbar-track{background:transparent}
  ::-webkit-scrollbar-thumb{background:#334155;border-radius:3px}
  .dot{display:inline-block;width:7px;height:7px;border-radius:50%;background:#94a3b8;animation:bounce 1.2s infinite}
  .dot:nth-child(2){animation-delay:.2s}
  .dot:nth-child(3){animation-delay:.4s}
  @keyframes bounce{0%,60%,100%{transform:translateY(0)}30%{transform:translateY(-7px)}}
</style>
</head>
<body class="bg-gray-900 text-gray-100 h-screen overflow-hidden">

<!-- LOGIN -->
<div id="loginPage" class="hidden fixed inset-0 flex items-center justify-center bg-gray-900 z-50">
  <div class="bg-gray-800 rounded-2xl p-8 w-full max-w-sm shadow-2xl border border-gray-700">
    <div class="text-center mb-7">
      <div class="text-3xl font-bold text-white">HP Engineering</div>
      <div class="text-gray-400 text-sm mt-1">AI Hub</div>
    </div>
    <div id="loginErr" class="hidden bg-red-900/40 border border-red-600 text-red-300 px-4 py-2.5 rounded-lg mb-4 text-sm"></div>
    <div class="space-y-3">
      <input id="loginUser" type="text" placeholder="사용자명"
        class="w-full bg-gray-700 border border-gray-600 rounded-xl px-4 py-3 text-white placeholder-gray-500 focus:outline-none focus:border-blue-500 transition"
        onkeydown="if(event.key==='Enter')login()">
      <input id="loginPw" type="password" placeholder="비밀번호"
        class="w-full bg-gray-700 border border-gray-600 rounded-xl px-4 py-3 text-white placeholder-gray-500 focus:outline-none focus:border-blue-500 transition"
        onkeydown="if(event.key==='Enter')login()">
      <button onclick="login()" class="w-full bg-blue-600 hover:bg-blue-700 text-white font-medium py-3 rounded-xl transition">
        로그인
      </button>
    </div>
  </div>
</div>

<!-- FORCE PASSWORD RESET -->
<div id="resetPage" class="hidden fixed inset-0 flex items-center justify-center bg-gray-900 z-50">
  <div class="bg-gray-800 rounded-2xl p-8 w-full max-w-sm shadow-2xl border border-gray-700">
    <div class="text-center mb-6">
      <div class="text-xl font-bold text-white">비밀번호 변경 필요</div>
      <div class="text-gray-400 text-sm mt-1">보안을 위해 새 비밀번호를 설정해 주세요</div>
    </div>
    <div id="resetErr" class="hidden bg-red-900/40 border border-red-600 text-red-300 px-4 py-2.5 rounded-lg mb-4 text-sm"></div>
    <div id="resetOk" class="hidden bg-green-900/40 border border-green-600 text-green-300 px-4 py-2.5 rounded-lg mb-4 text-sm"></div>
    <div class="space-y-3">
      <input id="curPw" type="password" placeholder="현재 비밀번호"
        class="w-full bg-gray-700 border border-gray-600 rounded-xl px-4 py-3 text-white placeholder-gray-500 focus:outline-none focus:border-blue-500 transition">
      <input id="newPw" type="password" placeholder="새 비밀번호 (8자 이상)"
        class="w-full bg-gray-700 border border-gray-600 rounded-xl px-4 py-3 text-white placeholder-gray-500 focus:outline-none focus:border-blue-500 transition">
      <input id="confirmPw" type="password" placeholder="새 비밀번호 확인"
        class="w-full bg-gray-700 border border-gray-600 rounded-xl px-4 py-3 text-white placeholder-gray-500 focus:outline-none focus:border-blue-500 transition"
        onkeydown="if(event.key==='Enter')changePassword()">
      <button onclick="changePassword()" class="w-full bg-blue-600 hover:bg-blue-700 text-white font-medium py-3 rounded-xl transition">
        비밀번호 변경
      </button>
    </div>
  </div>
</div>

<!-- MAIN -->
<div id="mainPage" class="hidden flex h-screen">
  <div class="w-60 bg-gray-800 border-r border-gray-700 flex flex-col flex-shrink-0">
    <div class="p-3 border-b border-gray-700">
      <button onclick="newChat()"
        class="w-full flex items-center gap-2 justify-center bg-blue-600 hover:bg-blue-700 text-white text-sm font-medium py-2.5 rounded-xl transition">
        <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 4v16m8-8H4"/>
        </svg>
        새 대화
      </button>
    </div>
    <div id="convList" class="flex-1 overflow-y-auto p-2 space-y-0.5"></div>
    <div class="p-3 border-t border-gray-700 space-y-1.5">
      <div id="adminBtn" class="hidden">
        <button onclick="openAdmin()"
          class="w-full flex items-center gap-2 text-sm text-gray-400 hover:text-white hover:bg-gray-700 px-3 py-2 rounded-lg transition">
          <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2"
              d="M10.325 4.317c.426-1.756 2.924-1.756 3.35 0a1.724 1.724 0 002.573 1.066c1.543-.94 3.31.826 2.37 2.37a1.724 1.724 0 001.065 2.572c1.756.426 1.756 2.924 0 3.35a1.724 1.724 0 00-1.066 2.573c.94 1.543-.826 3.31-2.37 2.37a1.724 1.724 0 00-2.572 1.065c-.426 1.756-2.924 1.756-3.35 0a1.724 1.724 0 00-2.573-1.066c-1.543.94-3.31-.826-2.37-2.37a1.724 1.724 0 00-1.065-2.572c-1.756-.426-1.756-2.924 0-3.35a1.724 1.724 0 001.066-2.573c-.94-1.543.826-3.31 2.37-2.37.996.608 2.296.07 2.572-1.065z"/>
            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15 12a3 3 0 11-6 0 3 3 0 016 0z"/>
          </svg>
          관리자 패널
        </button>
      </div>
      <div class="flex items-center justify-between px-1">
        <div class="flex items-center gap-2 min-w-0">
          <div id="avatar" class="w-7 h-7 bg-blue-600 rounded-full flex-shrink-0 flex items-center justify-center text-xs font-bold"></div>
          <span id="uname" class="text-sm text-gray-300 truncate"></span>
        </div>
        <button onclick="logout()" title="로그아웃" class="text-gray-500 hover:text-white transition flex-shrink-0">
          <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2"
              d="M17 16l4-4m0 0l-4-4m4 4H7m6 4v1a3 3 0 01-3 3H6a3 3 0 01-3-3V7a3 3 0 013-3h4a3 3 0 013 3v1"/>
          </svg>
        </button>
      </div>
    </div>
  </div>

  <div class="flex-1 flex flex-col min-w-0">
    <div id="msgArea" class="flex-1 overflow-y-auto px-4 py-6 space-y-5">
      <div id="welcome" class="flex items-center justify-center h-full text-center">
        <div>
          <div class="text-5xl mb-4">💬</div>
          <div class="text-xl font-medium text-gray-300">무엇을 도와드릴까요?</div>
          <div class="text-gray-500 text-sm mt-2">새 대화를 시작하거나 이전 대화를 선택하세요</div>
        </div>
      </div>
    </div>
    <div class="px-4 pb-4">
      <div class="bg-gray-800 border border-gray-600 focus-within:border-blue-500 rounded-2xl transition">
        <textarea id="input" rows="1" placeholder="메시지 입력... (Enter: 전송 / Shift+Enter: 줄바꿈)"
          class="w-full bg-transparent px-4 pt-3.5 pb-2 text-white placeholder-gray-500 focus:outline-none resize-none text-sm"
          oninput="autoResize(this)" onkeydown="handleKey(event)"></textarea>
        <div class="flex justify-end px-3 pb-3">
          <button id="sendBtn" onclick="sendMsg()"
            class="bg-blue-600 hover:bg-blue-700 disabled:bg-gray-600 disabled:cursor-not-allowed text-white p-2 rounded-lg transition">
            <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 19l9 2-9-18-9 18 9-2zm0 0v-8"/>
            </svg>
          </button>
        </div>
      </div>
    </div>
  </div>
</div>

<!-- ADMIN MODAL -->
<div id="adminModal" class="hidden fixed inset-0 bg-black/70 flex items-center justify-center z-40 p-4">
  <div class="bg-gray-800 border border-gray-700 rounded-2xl w-full max-w-3xl max-h-[88vh] flex flex-col shadow-2xl">
    <div class="flex items-center justify-between px-6 py-4 border-b border-gray-700">
      <div class="font-bold text-lg">관리자 패널</div>
      <button onclick="closeAdmin()" class="text-gray-400 hover:text-white transition">
        <svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12"/>
        </svg>
      </button>
    </div>
    <div class="flex border-b border-gray-700 px-2">
      <button id="tabU" onclick="switchTab('users')"
        class="px-5 py-3 text-sm font-medium border-b-2 border-blue-500 text-blue-400 transition">사용자 관리</button>
      <button id="tabK" onclick="switchTab('keys')"
        class="px-5 py-3 text-sm font-medium border-b-2 border-transparent text-gray-400 hover:text-white transition">API 키 관리</button>
    </div>

    <div id="usersTab" class="flex-1 overflow-y-auto p-5">
      <div class="flex items-center justify-between mb-4">
        <div class="font-medium text-sm text-gray-300">사용자 목록</div>
        <button onclick="showUserForm()" class="bg-blue-600 hover:bg-blue-700 text-white text-xs px-3 py-1.5 rounded-lg transition">+ 사용자 추가</button>
      </div>
      <div id="userForm" class="hidden bg-gray-700 rounded-xl p-4 mb-4">
        <div class="text-sm font-medium mb-3">새 사용자 생성</div>
        <div class="grid grid-cols-2 gap-2 mb-3">
          <input id="fUser" type="text" placeholder="사용자명"
            class="bg-gray-600 border border-gray-500 rounded-lg px-3 py-2 text-sm text-white placeholder-gray-400 focus:outline-none focus:border-blue-500">
          <input id="fPw" type="password" placeholder="초기 비밀번호"
            class="bg-gray-600 border border-gray-500 rounded-lg px-3 py-2 text-sm text-white placeholder-gray-400 focus:outline-none focus:border-blue-500">
          <input id="fEmail" type="email" placeholder="이메일 (선택)"
            class="bg-gray-600 border border-gray-500 rounded-lg px-3 py-2 text-sm text-white placeholder-gray-400 focus:outline-none focus:border-blue-500">
          <select id="fRole" class="bg-gray-600 border border-gray-500 rounded-lg px-3 py-2 text-sm text-white focus:outline-none focus:border-blue-500">
            <option value="user">일반 사용자</option>
            <option value="admin">관리자</option>
          </select>
        </div>
        <div class="flex gap-2">
          <button onclick="createUser()" class="bg-blue-600 hover:bg-blue-700 text-white text-xs px-4 py-1.5 rounded-lg transition">생성</button>
          <button onclick="hideUserForm()" class="bg-gray-600 hover:bg-gray-500 text-white text-xs px-4 py-1.5 rounded-lg transition">취소</button>
        </div>
      </div>
      <div id="userList" class="space-y-2"></div>
    </div>

    <div id="keysTab" class="hidden flex-1 overflow-y-auto p-5">
      <div class="flex items-center justify-between mb-4">
        <div class="font-medium text-sm text-gray-300">API 키 목록</div>
        <button onclick="showKeyForm()" class="bg-blue-600 hover:bg-blue-700 text-white text-xs px-3 py-1.5 rounded-lg transition">+ API 키 추가</button>
      </div>
      <div id="keyForm" class="hidden bg-gray-700 rounded-xl p-4 mb-4">
        <div class="text-sm font-medium mb-3">새 API 키 등록</div>
        <div class="grid grid-cols-2 gap-2 mb-3">
          <input id="kName" type="text" placeholder="키 이름 (예: Anthropic Main)"
            class="bg-gray-600 border border-gray-500 rounded-lg px-3 py-2 text-sm text-white placeholder-gray-400 focus:outline-none focus:border-blue-500">
          <select id="kProvider" class="bg-gray-600 border border-gray-500 rounded-lg px-3 py-2 text-sm text-white focus:outline-none focus:border-blue-500">
            <option value="anthropic">Anthropic</option>
            <option value="openai">OpenAI</option>
            <option value="gemini">Google Gemini</option>
            <option value="other">기타</option>
          </select>
          <input id="kValue" type="password" placeholder="API 키 값"
            class="col-span-2 bg-gray-600 border border-gray-500 rounded-lg px-3 py-2 text-sm text-white placeholder-gray-400 focus:outline-none focus:border-blue-500">
        </div>
        <div class="flex gap-2">
          <button onclick="createKey()" class="bg-blue-600 hover:bg-blue-700 text-white text-xs px-4 py-1.5 rounded-lg transition">등록</button>
          <button onclick="hideKeyForm()" class="bg-gray-600 hover:bg-gray-500 text-white text-xs px-4 py-1.5 rounded-lg transition">취소</button>
        </div>
      </div>
      <div id="keyList" class="space-y-2"></div>
    </div>
  </div>
</div>

<script>
let token = localStorage.getItem('token');
let me = null;
let convId = null;
let busy = false;

async function api(method, path, body) {
  const h = {'Content-Type':'application/json'};
  if(token) h['Authorization'] = 'Bearer '+token;
  const r = await fetch('/api'+path, {method, headers:h, body:body?JSON.stringify(body):undefined});
  if(r.status===401){logout();return null;}
  const j = await r.json();
  if(!r.ok) throw new Error(j.detail||'오류가 발생했습니다');
  return j;
}

async function login(){
  const u=document.getElementById('loginUser').value.trim();
  const p=document.getElementById('loginPw').value;
  const err=document.getElementById('loginErr');
  err.classList.add('hidden');
  if(!u||!p){err.textContent='아이디와 비밀번호를 입력해주세요';err.classList.remove('hidden');return;}
  try{
    const r=await api('POST','/auth/login',{username:u,password:p});
    token=r.access_token;
    localStorage.setItem('token',token);
    if(r.force_password_reset) showPage('resetPage');
    else await initMain();
  }catch(e){err.textContent=e.message;err.classList.remove('hidden');}
}

async function changePassword(){
  const cur=document.getElementById('curPw').value;
  const nw=document.getElementById('newPw').value;
  const cf=document.getElementById('confirmPw').value;
  const err=document.getElementById('resetErr');
  const ok=document.getElementById('resetOk');
  err.classList.add('hidden'); ok.classList.add('hidden');
  if(nw!==cf){err.textContent='새 비밀번호가 일치하지 않습니다';err.classList.remove('hidden');return;}
  if(nw.length<8){err.textContent='비밀번호는 8자 이상이어야 합니다';err.classList.remove('hidden');return;}
  try{
    await api('POST','/auth/change-password',{current_password:cur,new_password:nw});
    ok.textContent='변경 완료! 잠시 후 이동합니다...';
    ok.classList.remove('hidden');
    setTimeout(()=>initMain(),1200);
  }catch(e){err.textContent=e.message;err.classList.remove('hidden');}
}

function logout(){
  token=null; me=null; convId=null;
  localStorage.removeItem('token');
  showPage('loginPage');
}

async function initMain(){
  me=await api('GET','/auth/me');
  if(!me) return;
  document.getElementById('uname').textContent=me.username;
  document.getElementById('avatar').textContent=me.username[0].toUpperCase();
  if(me.role==='admin'||me.role==='superadmin') document.getElementById('adminBtn').classList.remove('hidden');
  showPage('mainPage');
  await loadConvs();
}

function showPage(id){
  ['loginPage','resetPage','mainPage'].forEach(x=>document.getElementById(x).classList.add('hidden'));
  document.getElementById(id).classList.remove('hidden');
}

async function loadConvs(){
  const convs=await api('GET','/chat/conversations');
  if(!convs) return;
  const el=document.getElementById('convList');
  el.innerHTML='';
  convs.forEach(c=>{
    const b=document.createElement('button');
    b.className='w-full text-left px-3 py-2 rounded-xl text-sm transition group flex items-center justify-between '+(c.id===convId?'bg-gray-700 text-white':'text-gray-400 hover:bg-gray-700 hover:text-white');
    b.innerHTML=`<span class="truncate flex-1">${esc(c.title)}</span>
      <span onclick="delConv(event,${c.id})" class="opacity-0 group-hover:opacity-100 text-gray-500 hover:text-red-400 ml-1 flex-shrink-0 cursor-pointer">
        <svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2"
            d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16"/>
        </svg>
      </span>`;
    b.onclick=e=>{if(!e.target.closest('span[onclick]'))loadConv(c.id);};
    el.appendChild(b);
  });
}

async function loadConv(id){
  convId=id;
  const msgs=await api('GET',`/chat/conversations/${id}/messages`);
  if(!msgs) return;
  const area=document.getElementById('msgArea');
  area.innerHTML='';
  msgs.forEach(m=>addMsg(m.role,m.content));
  area.scrollTop=area.scrollHeight;
  await loadConvs();
}

function newChat(){
  convId=null;
  document.getElementById('msgArea').innerHTML=`
    <div class="flex items-center justify-center h-full text-center">
      <div>
        <div class="text-5xl mb-4">💬</div>
        <div class="text-xl font-medium text-gray-300">무엇을 도와드릴까요?</div>
        <div class="text-gray-500 text-sm mt-2">새 대화를 시작하거나 이전 대화를 선택하세요</div>
      </div>
    </div>`;
  document.getElementById('input').focus();
}

async function delConv(e,id){
  e.stopPropagation();
  if(!confirm('이 대화를 삭제하시겠습니까?')) return;
  await api('DELETE',`/chat/conversations/${id}`);
  if(convId===id) newChat();
  await loadConvs();
}

async function sendMsg(){
  if(busy) return;
  const inp=document.getElementById('input');
  const content=inp.value.trim();
  if(!content) return;
  inp.value='';
  autoResize(inp);
  const welcome=document.getElementById('welcome');
  if(welcome) welcome.remove();
  addMsg('user',content);
  const tid='t'+Date.now();
  addTyping(tid);
  busy=true;
  document.getElementById('sendBtn').disabled=true;
  try{
    const r=await api('POST','/chat/send',{content,conversation_id:convId});
    if(r){
      convId=r.conversation_id;
      document.getElementById(tid)?.remove();
      addMsg('assistant',r.message.content);
      await loadConvs();
    }
  }catch(e){
    document.getElementById(tid)?.remove();
    addMsg('error','오류: '+e.message);
  }finally{
    busy=false;
    document.getElementById('sendBtn').disabled=false;
    inp.focus();
  }
}

function addMsg(role,content){
  const area=document.getElementById('msgArea');
  const d=document.createElement('div');
  if(role==='user'){
    d.className='flex justify-end';
    d.innerHTML=`<div class="max-w-[72%] bg-blue-600 rounded-2xl rounded-tr-sm px-4 py-3 text-sm whitespace-pre-wrap">${esc(content)}</div>`;
  }else if(role==='assistant'){
    d.className='flex gap-3';
    d.innerHTML=`<div class="w-7 h-7 bg-orange-500 rounded-full flex-shrink-0 flex items-center justify-center text-xs font-bold mt-0.5">AI</div>
      <div class="flex-1 max-w-[80%] msg-content text-sm text-gray-100">${marked.parse(content)}</div>`;
  }else{
    d.className='flex justify-center';
    d.innerHTML=`<div class="text-red-400 text-sm bg-red-900/30 px-4 py-2 rounded-lg">${esc(content)}</div>`;
  }
  area.appendChild(d);
  area.scrollTop=area.scrollHeight;
}

function addTyping(id){
  const area=document.getElementById('msgArea');
  const d=document.createElement('div');
  d.id=id; d.className='flex gap-3';
  d.innerHTML=`<div class="w-7 h-7 bg-orange-500 rounded-full flex-shrink-0 flex items-center justify-center text-xs font-bold mt-0.5">AI</div>
    <div class="flex items-center gap-1 py-2"><span class="dot"></span><span class="dot"></span><span class="dot"></span></div>`;
  area.appendChild(d);
  area.scrollTop=area.scrollHeight;
}

function handleKey(e){if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();sendMsg();}}
function autoResize(el){el.style.height='auto';el.style.height=Math.min(el.scrollHeight,180)+'px';}

function openAdmin(){document.getElementById('adminModal').classList.remove('hidden');loadUsers();}
function closeAdmin(){document.getElementById('adminModal').classList.add('hidden');}

function switchTab(t){
  document.getElementById('usersTab').classList.add('hidden');
  document.getElementById('keysTab').classList.add('hidden');
  document.getElementById('tabU').className='px-5 py-3 text-sm font-medium border-b-2 border-transparent text-gray-400 hover:text-white transition';
  document.getElementById('tabK').className='px-5 py-3 text-sm font-medium border-b-2 border-transparent text-gray-400 hover:text-white transition';
  document.getElementById(t==='users'?'usersTab':'keysTab').classList.remove('hidden');
  document.getElementById(t==='users'?'tabU':'tabK').className='px-5 py-3 text-sm font-medium border-b-2 border-blue-500 text-blue-400 transition';
  if(t==='users') loadUsers(); else loadKeys();
}

async function loadUsers(){
  const users=await api('GET','/users');
  if(!users) return;
  const el=document.getElementById('userList');
  el.innerHTML='';
  const roleLabel={superadmin:'최고 관리자',admin:'관리자',user:'일반 사용자'};
  const roleCls={superadmin:'bg-purple-900/40 text-purple-300 border-purple-600',admin:'bg-blue-900/40 text-blue-300 border-blue-600',user:'bg-gray-700 text-gray-300 border-gray-600'};
  users.forEach(u=>{
    const d=document.createElement('div');
    d.className='bg-gray-700 rounded-xl px-4 py-3 flex items-center justify-between gap-3';
    d.innerHTML=`<div class="flex items-center gap-2.5 min-w-0">
        <div class="w-8 h-8 bg-blue-600 rounded-full flex-shrink-0 flex items-center justify-center text-xs font-bold">${u.username[0].toUpperCase()}</div>
        <div class="min-w-0">
          <div class="text-sm font-medium truncate">${esc(u.username)}</div>
          <div class="text-xs text-gray-400 truncate">${u.email||'이메일 없음'}</div>
        </div>
        <span class="text-xs border px-2 py-0.5 rounded-full flex-shrink-0 ${roleCls[u.role]||roleCls.user}">${roleLabel[u.role]||u.role}</span>
        ${u.force_password_reset?'<span class="text-xs text-yellow-400 bg-yellow-900/30 px-2 py-0.5 rounded-full border border-yellow-700 flex-shrink-0">PW 변경 필요</span>':''}
      </div>
      <div class="flex-shrink-0">
        ${u.role!=='superadmin'?`<button onclick="toggleUser(${u.id})"
          class="text-xs px-3 py-1 rounded-lg border transition ${u.is_active?'border-red-700 text-red-400 hover:bg-red-900/30':'border-green-700 text-green-400 hover:bg-green-900/30'}">
          ${u.is_active?'비활성화':'활성화'}
        </button>`:''}
      </div>`;
    el.appendChild(d);
  });
}

function showUserForm(){document.getElementById('userForm').classList.remove('hidden');}
function hideUserForm(){document.getElementById('userForm').classList.add('hidden');}

async function createUser(){
  const u=document.getElementById('fUser').value.trim();
  const p=document.getElementById('fPw').value;
  const e=document.getElementById('fEmail').value.trim();
  const r=document.getElementById('fRole').value;
  if(!u||!p){alert('사용자명과 비밀번호를 입력해주세요');return;}
  try{
    await api('POST','/users',{username:u,password:p,email:e||null,role:r});
    document.getElementById('fUser').value='';
    document.getElementById('fPw').value='';
    document.getElementById('fEmail').value='';
    hideUserForm();
    await loadUsers();
  }catch(e){alert(e.message);}
}

async function toggleUser(id){
  try{await api('PATCH',`/users/${id}/toggle-active`);await loadUsers();}
  catch(e){alert(e.message);}
}

async function loadKeys(){
  const keys=await api('GET','/keys');
  if(!keys) return;
  const el=document.getElementById('keyList');
  el.innerHTML='';
  const pLabel={anthropic:'Anthropic',openai:'OpenAI',gemini:'Google Gemini',other:'기타'};
  const pCls={anthropic:'bg-orange-900/40 text-orange-300 border-orange-600',openai:'bg-green-900/40 text-green-300 border-green-600',gemini:'bg-blue-900/40 text-blue-300 border-blue-600',other:'bg-gray-700 text-gray-300 border-gray-600'};
  keys.forEach(k=>{
    const d=document.createElement('div');
    d.className='bg-gray-700 rounded-xl px-4 py-3 flex items-center justify-between gap-3';
    d.innerHTML=`<div class="flex items-center gap-2.5 min-w-0">
        <span class="text-xs border px-2 py-0.5 rounded-full flex-shrink-0 ${pCls[k.provider]||pCls.other}">${pLabel[k.provider]||k.provider}</span>
        <div class="min-w-0">
          <div class="text-sm font-medium truncate">${esc(k.name)}</div>
          <div class="text-xs text-gray-400 font-mono truncate">${esc(k.key_value)}</div>
        </div>
        <span class="text-xs flex-shrink-0 ${k.is_active?'text-green-400':'text-gray-500'}">${k.is_active?'활성':'비활성'}</span>
      </div>
      <div class="flex gap-1.5 flex-shrink-0">
        <button onclick="toggleKey(${k.id})"
          class="text-xs px-2.5 py-1 rounded-lg border transition ${k.is_active?'border-yellow-700 text-yellow-400 hover:bg-yellow-900/30':'border-green-700 text-green-400 hover:bg-green-900/30'}">
          ${k.is_active?'비활성화':'활성화'}
        </button>
        <button onclick="delKey(${k.id})"
          class="text-xs px-2.5 py-1 rounded-lg border border-red-700 text-red-400 hover:bg-red-900/30 transition">삭제</button>
      </div>`;
    el.appendChild(d);
  });
}

function showKeyForm(){document.getElementById('keyForm').classList.remove('hidden');}
function hideKeyForm(){document.getElementById('keyForm').classList.add('hidden');}

async function createKey(){
  const n=document.getElementById('kName').value.trim();
  const p=document.getElementById('kProvider').value;
  const v=document.getElementById('kValue').value.trim();
  if(!n||!v){alert('키 이름과 API 키 값을 입력해주세요');return;}
  try{
    await api('POST','/keys',{name:n,provider:p,key_value:v});
    document.getElementById('kName').value='';
    document.getElementById('kValue').value='';
    hideKeyForm();
    await loadKeys();
  }catch(e){alert(e.message);}
}

async function toggleKey(id){
  try{await api('PATCH',`/keys/${id}/toggle`);await loadKeys();}
  catch(e){alert(e.message);}
}

async function delKey(id){
  if(!confirm('이 API 키를 삭제하시겠습니까?')) return;
  try{await api('DELETE',`/keys/${id}`);await loadKeys();}
  catch(e){alert(e.message);}
}

function esc(t){return(t||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');}

(async()=>{
  if(token){
    try{
      me=await api('GET','/auth/me');
      if(!me) return showPage('loginPage');
      if(me.force_password_reset) return showPage('resetPage');
      document.getElementById('uname').textContent=me.username;
      document.getElementById('avatar').textContent=me.username[0].toUpperCase();
      if(me.role==='admin'||me.role==='superadmin') document.getElementById('adminBtn').classList.remove('hidden');
      showPage('mainPage');
      await loadConvs();
    }catch{showPage('loginPage');}
  }else{
    showPage('loginPage');
  }
})();
</script>
</body>
</html>
```

---

## 참고 사항

### 서버 현황
- **PostgreSQL**: `n8n-postgres` 컨테이너 (포트 5432, `n8n_default` 네트워크)
- **n8n**: `n8n` 컨테이너 (포트 5678)
- **claude-chat**: `claude-chat` 컨테이너 (포트 3899)

### 역할 구조
- `superadmin`: 최고 관리자 (초기 비밀번호는 `.env`의 `SUPERADMIN_INITIAL_PASSWORD`로 설정, 첫 로그인 시 강제 변경)
- `admin`: superadmin이 생성, admin이 일반 사용자 생성 가능
- `user`: 일반 직원

### IP 화이트리스트
`.env`의 `ALLOWED_IPS`에서 수정 (CIDR 표기 가능, 쉼표로 구분)

### Anthropic API 키 등록
1. superadmin 또는 admin으로 로그인
2. 관리자 패널 > API 키 관리 탭
3. provider: `anthropic`, 키 값 입력 후 등록

### 도메인 연결 (Nginx 예시)
```nginx
server {
    server_name chatting.도메인.kr apikey.도메인.kr;
    location / {
        proxy_pass http://localhost:3899;
        proxy_set_header X-Forwarded-For $remote_addr;
        proxy_set_header Host $host;
    }
}
```
