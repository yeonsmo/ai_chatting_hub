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


# ---------- 첨부파일 ----------

class AttachmentResponse(BaseModel):
    id: int
    filename: str
    content_type: str
    size_bytes: int
    has_text: bool = False
    created_at: datetime

    class Config:
        from_attributes = True


# ---------- 채팅 ----------

class MessageCreate(BaseModel):
    content: str
    conversation_id: Optional[int] = None
    project_id: Optional[int] = None          # 새 대화를 프로젝트 안에서 시작할 때
    model: Optional[str] = "sonnet"
    attachment_ids: list[int] = []


class MessageResponse(BaseModel):
    id: int
    role: str
    content: str
    model: Optional[str] = None
    provider: Optional[str] = None
    created_at: datetime
    attachments: list[AttachmentResponse] = []

    class Config:
        from_attributes = True


class ConversationResponse(BaseModel):
    id: int
    title: str
    project_id: Optional[int] = None
    is_archived: bool = False
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class ConversationUpdate(BaseModel):
    title: Optional[str] = None


class ChatResponse(BaseModel):
    conversation_id: int
    conversation_title: str
    message: MessageResponse


# ---------- 프로젝트 ----------

class ProjectCreate(BaseModel):
    name: str
    description: str = ""
    instructions: str = ""


class ProjectUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    instructions: Optional[str] = None


class ProjectResponse(BaseModel):
    id: int
    name: str
    description: str
    instructions: str
    created_at: datetime
    updated_at: datetime
    file_count: int = 0
    conversation_count: int = 0

    class Config:
        from_attributes = True


# ---------- API 키 ----------

class APIKeyCreate(BaseModel):
    name: str
    provider: str
    key_value: str


class APIKeyResponse(BaseModel):
    id: int
    name: str
    provider: str
    key_value: str  # 마스킹된 값만 담아 반환
    is_active: bool
    created_at: datetime


# ---------- 관리자(감사) ----------

class AdminConversationResponse(BaseModel):
    id: int
    title: str
    user_id: int
    username: str
    project_id: Optional[int] = None
    is_archived: bool
    message_count: int
    created_at: datetime
    updated_at: datetime


class UsageLogResponse(BaseModel):
    id: int
    user_id: Optional[int]
    username: str
    action: str
    conversation_id: Optional[int]
    model: Optional[str]
    provider: Optional[str]
    input_tokens: Optional[int]
    output_tokens: Optional[int]
    status: str
    detail: Optional[str]
    duration_ms: Optional[int]
    client_ip: Optional[str]
    created_at: datetime

    class Config:
        from_attributes = True
