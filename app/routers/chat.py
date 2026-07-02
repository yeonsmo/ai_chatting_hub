from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
import anthropic
from openai import AsyncOpenAI
from app.config import settings
from app.database import get_db
from app.dependencies import get_current_user
from app.models import User, Conversation, Message, APIKey
from app.schemas import MessageCreate, ChatResponse, ConversationResponse, MessageResponse

router = APIRouter(prefix="/chat", tags=["chat"])


CLAUDE_MODELS = {
    "sonnet":                    "claude-sonnet-4-6",
    "opus":                      "claude-opus-4-6",
    "haiku":                     "claude-haiku-4-5-20251001",
    "claude-sonnet-4-6":         "claude-sonnet-4-6",
    "claude-opus-4-6":           "claude-opus-4-6",
    "claude-haiku-4-5-20251001": "claude-haiku-4-5-20251001",
}

GABIA_MODELS = {
    # OpenAI
    "gpt-5-pro":   "gpt-5.4-pro",
    "gpt-5":       "gpt-5.2",
    "o4-mini":     "o4-mini",
    "codex":       "gpt-5.3-codex",
    # DeepSeek
    "deepseek":    "deepseek-r1-0528",
    # Google
    "gemini":      "gemini-3.1-flash-lite-preview",
    # Alibaba
    "qwen":        "qwen3.5-122b-a10b",
    "qwen-plus":   "qwen3.6-plus",
    # Meta
    "llama":       "llama-3.2-11b-vision",
    # Moonshot
    "kimi":        "kimi-k2-instruct",
    "kimi-think":  "kimi-k2-thinking",
    # MiniMax
    "minimax":     "minimax-m2.1",
    # Perplexity
    "sonar":       "sonar-pro-search",
    "sonar-deep":  "sonar-deep-research",
    # ZAI
    "glm":         "glm-4.7",
    # Xiaomi
    "mimo":        "mimo-v2.5-pro",
}


async def get_anthropic_key(db: AsyncSession) -> str:
    result = await db.execute(
        select(APIKey).where(APIKey.provider == "anthropic", APIKey.is_active == True).limit(1)
    )
    key_obj = result.scalars().first()
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

    # 모델 라우팅: Claude vs 가비아
    requested = request.model or "sonnet"
    if requested in GABIA_MODELS:
        model_id = GABIA_MODELS[requested]
        if not settings.gabia_api_key:
            raise HTTPException(status_code=500, detail="가비아 API 키가 설정되지 않았습니다.")
        client = AsyncOpenAI(api_key=settings.gabia_api_key, base_url=f"{settings.ai_hub_base_url}/v1")
        response = await client.chat.completions.create(
            model=model_id,
            max_tokens=4096,
            messages=claude_messages,
        )
        assistant_content = response.choices[0].message.content
    else:
        model_id = CLAUDE_MODELS.get(requested, "claude-sonnet-4-6")
        api_key = await get_anthropic_key(db)
        claude_client = anthropic.AsyncAnthropic(api_key=api_key)
        response = await claude_client.messages.create(
            model=model_id,
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
