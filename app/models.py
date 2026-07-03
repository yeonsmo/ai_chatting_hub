import enum
from datetime import datetime
from sqlalchemy import Column, Integer, String, Boolean, DateTime, Text, ForeignKey, Enum, BigInteger
from sqlalchemy.orm import relationship
from app.database import Base


class UserRole(str, enum.Enum):
    """역할 계층. 레벨은 app/roles.py의 ROLE_LEVELS에서 정의(확장 시 양쪽 모두 추가)."""
    superadmin = "superadmin"  # 최고 관리자
    admin = "admin"            # 관리자
    manager = "manager"        # 담당자
    user = "user"              # 일반 사용자


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    username = Column(String(50), unique=True, index=True, nullable=False)
    email = Column(String(100), unique=True, nullable=True)
    hashed_password = Column(String(255), nullable=False)
    role = Column(Enum(UserRole), default=UserRole.user, nullable=False)
    is_active = Column(Boolean, default=True)
    force_password_reset = Column(Boolean, default=False)
    token_version = Column(Integer, default=0, nullable=False)  # 증가 시 기존 JWT 전부 무효화
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
    kind = Column(String(20), default="upload", nullable=False)  # upload | generated
    origin = Column(String(80), nullable=True)        # 생성 출처(도구명/모델키)
    created_at = Column(DateTime, default=datetime.utcnow)

    message = relationship("Message", back_populates="attachments", foreign_keys=[message_id])
    project = relationship("Project", back_populates="files", foreign_keys=[project_id])


class APIKey(Base):
    __tablename__ = "api_keys"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(100), nullable=False)
    provider = Column(String(50), nullable=False)  # anthropic, gabia, openai, azure, gemini, bedrock, ...
    key_value = Column(String(500), nullable=False)
    # 프로바이더별 추가 설정(JSON): azure={endpoint, api_version}, bedrock={aws_secret_key, region} 등
    extra = Column(Text, nullable=True)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    created_by = Column(Integer, ForeignKey("users.id"), nullable=False)


class ModelRoute(Base):
    """모델 키 → 어느 프로바이더의 어떤 모델을 호출할지 지정 (관리자 패널에서 편집)."""
    __tablename__ = "model_routes"

    id = Column(Integer, primary_key=True, index=True)
    key = Column(String(80), unique=True, nullable=False)          # 프론트에서 쓰는 모델 키 (예: sonnet)
    label = Column(String(120), nullable=False)                    # 표시 이름
    provider = Column(String(50), nullable=False)                  # anthropic/gabia/openai/azure/gemini/bedrock
    provider_model_id = Column(String(200), nullable=False)        # 실제 모델 ID(azure는 deployment 이름)
    kind = Column(String(20), default="chat", nullable=False)      # chat | image
    description = Column(String(200), default="")
    min_role = Column(String(30), default="user", nullable=False)  # 이 모델을 쓸 수 있는 최소 역할
    enabled = Column(Boolean, default=True, nullable=False)
    sort = Column(Integer, default=100)


class Integration(Base):
    """외부 시스템 연동(하이웍스, 구글, 사내 API 등). 스킬이 이 연동의 인증으로 호출된다."""
    __tablename__ = "integrations"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(100), nullable=False)          # 표시 이름 (예: 하이웍스)
    kind = Column(String(50), default="custom")         # hiworks/google/slack/custom ...
    base_url = Column(String(500), nullable=False)      # https://api.example.com
    auth_type = Column(String(20), default="bearer")    # bearer / header / query / none
    auth_key = Column(String(100), default="Authorization")  # header 이름 또는 query 파라미터 이름
    credential = Column(String(1000), default="")       # API 키/토큰
    extra_headers = Column(Text, nullable=True)         # 추가 헤더 JSON
    is_active = Column(Boolean, default=True, nullable=False)
    created_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    skills = relationship("Skill", back_populates="integration", cascade="all, delete-orphan")


class Skill(Base):
    """AI가 도구(tool)로 호출할 수 있는 API 스킬. min_role로 권한별 사용 제한."""
    __tablename__ = "skills"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(64), unique=True, nullable=False)   # 도구 이름 (영문/숫자/_)
    title = Column(String(120), nullable=False)              # 표시 이름 (예: 하이웍스 결재 조회)
    description = Column(Text, nullable=False)               # 모델에게 주는 설명(언제 쓰는지)
    integration_id = Column(Integer, ForeignKey("integrations.id", ondelete="CASCADE"), nullable=False)
    method = Column(String(10), default="GET")               # GET/POST/PUT/DELETE
    path = Column(String(500), nullable=False)               # /v1/users/{user_id} 형태, {param} 치환
    query_template = Column(Text, nullable=True)             # JSON: {"q": "{keyword}"}
    body_template = Column(Text, nullable=True)              # JSON 문자열, {param} 치환
    params_schema = Column(Text, nullable=True)              # JSON: [{name,type,description,required}]
    min_role = Column(String(30), default="manager", nullable=False)  # 권한별 사용 제한
    timeout_s = Column(Integer, default=20)
    is_active = Column(Boolean, default=True, nullable=False)
    created_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    integration = relationship("Integration", back_populates="skills")


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
