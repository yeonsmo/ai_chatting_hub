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
    model: Optional[str] = "claude-sonnet-4-6"


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
