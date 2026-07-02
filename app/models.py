import enum
from datetime import datetime
from sqlalchemy import Column, Integer, String, Boolean, DateTime, Text, ForeignKey, Enum, BigInteger
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


class Project(Base):
    """클로드 '프로젝트'와 같은 개념: 지침 + 지식 파일 + 소속 대화 묶음"""
    __tablename__ = "projects"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    name = Column(String(120), nullable=False)
    description = Column(String(500), default="")
    instructions = Column(Text, default="")  # 프로젝트 커스텀 지침(시스템 프롬프트)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    conversations = relationship("Conversation", back_populates="project")
    files = relationship("Attachment", back_populates="project",
                         foreign_keys="Attachment.project_id", cascade="all, delete-orphan")


class Conversation(Base):
    __tablename__ = "conversations"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    project_id = Column(Integer, ForeignKey("projects.id", ondelete="SET NULL"), nullable=True, index=True)
    title = Column(String(255), default="새 대화")
    is_archived = Column(Boolean, default=False, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    user = relationship("User", back_populates="conversations", foreign_keys=[user_id])
    project = relationship("Project", back_populates="conversations")
    messages = relationship("Message", back_populates="conversation", cascade="all, delete-orphan")


class Message(Base):
    __tablename__ = "messages"

    id = Column(Integer, primary_key=True, index=True)
    conversation_id = Column(Integer, ForeignKey("conversations.id"), nullable=False)
    role = Column(String(20), nullable=False)  # "user" or "assistant"
    content = Column(Text, nullable=False)
    model = Column(String(80), nullable=True)     # assistant 메시지의 모델 키 (예: sonnet)
    provider = Column(String(40), nullable=True)  # anthropic / gabia
    created_at = Column(DateTime, default=datetime.utcnow)

    conversation = relationship("Conversation", back_populates="messages")
    # 메시지 삭제 시 첨부 행 제거는 DB의 ON DELETE CASCADE에 위임
    attachments = relationship("Attachment", back_populates="message",
                               foreign_keys="Attachment.message_id", passive_deletes=True)


class Attachment(Base):
    """업로드 파일. message_id가 있으면 메시지 첨부, project_id가 있으면 프로젝트 지식 파일."""
    __tablename__ = "attachments"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    message_id = Column(Integer, ForeignKey("messages.id", ondelete="CASCADE"), nullable=True, index=True)
    project_id = Column(Integer, ForeignKey("projects.id", ondelete="CASCADE"), nullable=True, index=True)
    filename = Column(String(255), nullable=False)
    content_type = Column(String(120), default="application/octet-stream")
    size_bytes = Column(BigInteger, default=0)
    stored_name = Column(String(80), nullable=False)  # 디스크 상 파일명(uuid)
    text_content = Column(Text, nullable=True)        # 추출된 텍스트(모델 컨텍스트용)
    created_at = Column(DateTime, default=datetime.utcnow)

    message = relationship("Message", back_populates="attachments", foreign_keys=[message_id])
    project = relationship("Project", back_populates="files", foreign_keys=[project_id])


class APIKey(Base):
    __tablename__ = "api_keys"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(100), nullable=False)
    provider = Column(String(50), nullable=False)  # anthropic, gabia, openai, gemini, etc.
    key_value = Column(String(500), nullable=False)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    created_by = Column(Integer, ForeignKey("users.id"), nullable=False)


class UsageLog(Base):
    """감사 로그: 누가 언제 어떤 모델로 무엇을 했는지 (superadmin 조회용)"""
    __tablename__ = "usage_logs"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    username = Column(String(50), nullable=False)  # 사용자 삭제 후에도 로그 보존
    action = Column(String(30), nullable=False, index=True)  # login / login_failed / chat
    conversation_id = Column(Integer, nullable=True)
    model = Column(String(80), nullable=True)
    provider = Column(String(40), nullable=True)
    input_tokens = Column(Integer, nullable=True)
    output_tokens = Column(Integer, nullable=True)
    status = Column(String(20), default="success")  # success / error
    detail = Column(String(500), nullable=True)     # 에러 메시지 등
    duration_ms = Column(Integer, nullable=True)
    client_ip = Column(String(64), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)
