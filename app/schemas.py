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
    kind: str = "upload"        # upload | generated
    origin: Optional[str] = None
    workflow_status: Optional[str] = None   # None | held | submitted
    approval_status: Optional[str] = None   # pending | approved | rejected
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


class SkillUse(BaseModel):
    name: str
    title: str
    status: str  # success / error


class ChatResponse(BaseModel):
    conversation_id: int
    conversation_title: str
    used_skills: list[SkillUse] = []
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
    extra: Optional[dict] = None  # azure: {endpoint, api_version} / bedrock: {aws_secret_key, region}


class APIKeyResponse(BaseModel):
    id: int
    name: str
    provider: str
    key_value: str  # 마스킹된 값만 담아 반환
    extra_keys: list[str] = []  # 등록된 추가 설정 키 이름만 노출(값은 비공개)
    is_active: bool
    created_at: datetime


# ---------- 모델 라우팅 ----------

class ModelRouteUpsert(BaseModel):
    key: str
    label: str
    provider: str
    provider_model_id: str
    kind: str = "chat"          # chat | image
    description: str = ""
    min_role: str = "user"
    enabled: bool = True
    sort: int = 100


class ModelRouteResponse(ModelRouteUpsert):
    id: int

    class Config:
        from_attributes = True


# ---------- 연동/스킬 ----------

class IntegrationCreate(BaseModel):
    name: str
    kind: str = "custom"
    base_url: str
    auth_type: str = "bearer"      # bearer / header / query / none
    auth_key: str = "Authorization"
    credential: str = ""
    extra_headers: Optional[dict] = None


class IntegrationUpdate(BaseModel):
    name: Optional[str] = None
    kind: Optional[str] = None
    base_url: Optional[str] = None
    auth_type: Optional[str] = None
    auth_key: Optional[str] = None
    credential: Optional[str] = None  # 빈 문자열이면 유지
    extra_headers: Optional[dict] = None
    is_active: Optional[bool] = None


class IntegrationResponse(BaseModel):
    id: int
    name: str
    kind: str
    base_url: str
    auth_type: str
    auth_key: str
    has_credential: bool = False
    is_active: bool
    skill_count: int = 0
    created_at: datetime


class SkillParam(BaseModel):
    name: str
    type: str = "string"
    description: str = ""
    required: bool = False


class SkillCreate(BaseModel):
    name: str                       # 도구 이름 (영문/숫자/_/-)
    title: str
    description: str
    integration_id: int
    method: str = "GET"
    path: str
    query_template: Optional[dict] = None
    body_template: Optional[str] = None
    params: list[SkillParam] = []
    min_role: str = "manager"
    timeout_s: int = 20
    is_active: bool = True


class SkillUpdate(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None
    integration_id: Optional[int] = None
    method: Optional[str] = None
    path: Optional[str] = None
    query_template: Optional[dict] = None
    body_template: Optional[str] = None
    params: Optional[list[SkillParam]] = None
    min_role: Optional[str] = None
    timeout_s: Optional[int] = None
    is_active: Optional[bool] = None


class SkillResponse(BaseModel):
    id: int
    name: str
    title: str
    description: str
    integration_id: int
    integration_name: str = ""
    method: str
    path: str
    query_template: Optional[dict] = None
    body_template: Optional[str] = None
    params: list[SkillParam] = []
    min_role: str
    timeout_s: int
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
    request_params: Optional[str] = None
    response_preview: Optional[str] = None
    created_at: datetime

    class Config:
        from_attributes = True
