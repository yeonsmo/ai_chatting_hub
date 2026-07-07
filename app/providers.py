"""프로바이더 디스패치 계층.

ModelRoute.provider 값에 따라 실제 호출처를 결정한다.
- anthropic : Anthropic 공식 API
- bedrock   : AWS Bedrock의 Claude (anthropic SDK의 AnthropicBedrock)
- gabia     : 가비아 AI Hub (OpenAI 호환)
- openai    : OpenAI 공식 API
- azure     : Azure OpenAI (extra: endpoint, api_version / model_id는 deployment 이름)
- gemini    : Google Gemini REST API

새 프로바이더 추가: PROVIDERS에 등록하고 run_chat의 분기에 구현.
스킬(tool) 지원: anthropic/bedrock은 tool-use, OpenAI 계열은 function-calling.
"""
import base64
import json
from dataclasses import dataclass, field
from typing import Awaitable, Callable

import httpx
import anthropic
import openai
from openai import AsyncOpenAI, AsyncAzureOpenAI
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import APIKey

MAX_TOKENS = 4096
MAX_TOOL_ROUNDS = 5
REQUEST_TIMEOUT = 180.0

# 프로바이더 메타: label(표시), env_fallback(.env 키 사용 가능 여부), extra_fields(키 등록 시 추가 설정)
PROVIDERS = {
    "anthropic": {"label": "Anthropic (Claude)", "extra_fields": []},
    "gabia":     {"label": "가비아 AI Hub",       "extra_fields": []},
    "openai":    {"label": "OpenAI 공식",         "extra_fields": []},
    "azure":     {"label": "Azure OpenAI",        "extra_fields": ["endpoint", "api_version"]},
    "gemini":    {"label": "Google Gemini",       "extra_fields": []},
    "bedrock":   {"label": "AWS Bedrock",         "extra_fields": ["aws_secret_key", "region"]},
}

ANTHROPIC_FAMILY = {"anthropic", "bedrock"}
OPENAI_FAMILY = {"gabia", "openai", "azure"}
# 이미지 생성을 지원하는 프로바이더(대화 모델과 별개의 이미지 엔드포인트 사용)
IMAGE_PROVIDERS = {"gabia", "openai", "azure", "gemini"}
MAX_IMAGE_OUT_BYTES = 8 * 1024 * 1024


@dataclass
class SkillCall:
    name: str
    title: str
    status: str  # success / error
    detail: str = ""


@dataclass
class ChatOutcome:
    text: str
    input_tokens: int | None = None
    output_tokens: int | None = None
    used_skills: list[SkillCall] = field(default_factory=list)


@dataclass
class ToolContext:
    """chat.py가 구성해 넘기는 스킬 실행 컨텍스트."""
    anthropic_tools: list      # anthropic tools 스키마
    openai_tools: list         # openai function-calling 스키마
    titles: dict               # name → 표시 이름
    executor: Callable[[str, dict], Awaitable[str]]  # (skill_name, params) → 결과 텍스트


async def resolve_credentials(db: AsyncSession, provider: str) -> tuple[str, dict]:
    """관리자 패널 등록 키 우선, anthropic/gabia는 .env 폴백."""
    result = await db.execute(
        select(APIKey)
        .where(APIKey.provider == provider, APIKey.is_active == True)
        .order_by(APIKey.id.desc())
        .limit(1)
    )
    key_obj = result.scalars().first()
    if key_obj:
        try:
            extra = json.loads(key_obj.extra) if key_obj.extra else {}
        except (ValueError, TypeError):
            extra = {}
        return key_obj.key_value, extra

    fallback = {"anthropic": settings.anthropic_api_key, "gabia": settings.gabia_api_key}.get(provider, "")
    if not fallback:
        label = PROVIDERS.get(provider, {}).get("label", provider)
        raise HTTPException(
            status_code=500,
            detail=f"{label} API 키가 설정되지 않았습니다. 관리자 패널 > API 키에서 등록하세요.",
        )
    return fallback, {}


def friendly_api_error(e: Exception) -> str:
    if isinstance(e, (anthropic.APIStatusError, openai.APIStatusError)):
        try:
            body = e.response.json()
            msg = body.get("error", {}).get("message") or str(body)[:200]
        except Exception:
            msg = str(e)[:200]
        return f"AI 응답 실패 (HTTP {e.status_code}): {msg}"
    if isinstance(e, (anthropic.APIConnectionError, openai.APIConnectionError, httpx.ConnectError)):
        return "AI 서버에 연결할 수 없습니다. 잠시 후 다시 시도해주세요."
    if isinstance(e, httpx.HTTPStatusError):
        return f"AI 응답 실패 (HTTP {e.response.status_code}): {e.response.text[:200]}"
    return f"AI 응답 실패: {str(e)[:200]}"


def _empty_response_error():
    return HTTPException(status_code=502, detail="모델이 빈 응답을 반환했습니다. 다시 시도해주세요.")


# ---------------- Anthropic 계열 (anthropic / bedrock) ----------------

def _make_anthropic_client(provider: str, key: str, extra: dict):
    if provider == "bedrock":
        region = extra.get("region") or "us-east-1"
        secret = extra.get("aws_secret_key") or ""
        if not secret:
            raise HTTPException(status_code=500, detail="Bedrock 키에 aws_secret_key 설정이 필요합니다 (키 등록 시 추가 설정).")
        return anthropic.AsyncAnthropicBedrock(
            aws_access_key=key, aws_secret_key=secret, aws_region=region, timeout=REQUEST_TIMEOUT,
        )
    return anthropic.AsyncAnthropic(api_key=key, timeout=REQUEST_TIMEOUT)


def _anthropic_assistant_content(blocks) -> list:
    """assistant 응답 블록을 '입력으로 다시 보낼 수 있는' 최소 필드만 남겨 재구성한다.
    SDK 신버전이 text 블록에 붙이는 parsed_output 등 출력 전용 필드를 그대로 되돌려
    보내면 Anthropic이 400(Extra inputs are not permitted)을 내므로 화이트리스트로 정리."""
    out = []
    for b in blocks:
        t = getattr(b, "type", "")
        if t == "text":
            out.append({"type": "text", "text": getattr(b, "text", "")})
        elif t == "tool_use":
            out.append({"type": "tool_use", "id": b.id, "name": b.name, "input": b.input})
        elif t == "thinking":
            d = {"type": "thinking", "thinking": getattr(b, "thinking", "")}
            sig = getattr(b, "signature", None)
            if sig:
                d["signature"] = sig
            out.append(d)
        elif t == "redacted_thinking":
            out.append({"type": "redacted_thinking", "data": getattr(b, "data", "")})
        else:
            d = b.model_dump()
            d.pop("parsed_output", None)
            out.append(d)
    return out


async def _run_anthropic(provider: str, model_id: str, key: str, extra: dict,
                         system_prompt: str | None, messages: list,
                         tool_ctx: ToolContext | None) -> ChatOutcome:
    client = _make_anthropic_client(provider, key, extra)
    kwargs = {"model": model_id, "max_tokens": MAX_TOKENS, "messages": list(messages)}
    if system_prompt:
        kwargs["system"] = system_prompt
    if tool_ctx and tool_ctx.anthropic_tools:
        kwargs["tools"] = tool_ctx.anthropic_tools

    outcome = ChatOutcome(text="")
    in_tok = out_tok = 0

    response = await client.messages.create(**kwargs)
    rounds = 0
    while getattr(response, "stop_reason", None) == "tool_use" and tool_ctx and rounds < MAX_TOOL_ROUNDS:
        rounds += 1
        usage = getattr(response, "usage", None)
        in_tok += getattr(usage, "input_tokens", 0) or 0
        out_tok += getattr(usage, "output_tokens", 0) or 0

        kwargs["messages"].append({"role": "assistant", "content": _anthropic_assistant_content(response.content)})
        tool_results = []
        for block in response.content:
            if getattr(block, "type", "") != "tool_use":
                continue
            name = block.name
            try:
                result_text = await tool_ctx.executor(name, dict(block.input or {}))
                outcome.used_skills.append(SkillCall(name=name, title=tool_ctx.titles.get(name, name), status="success"))
            except Exception as e:
                result_text = f"오류: {str(e)[:300]}"
                outcome.used_skills.append(SkillCall(name=name, title=tool_ctx.titles.get(name, name),
                                                     status="error", detail=str(e)[:200]))
            tool_results.append({"type": "tool_result", "tool_use_id": block.id, "content": result_text[:20000]})
        kwargs["messages"].append({"role": "user", "content": tool_results})
        response = await client.messages.create(**kwargs)

    usage = getattr(response, "usage", None)
    in_tok += getattr(usage, "input_tokens", 0) or 0
    out_tok += getattr(usage, "output_tokens", 0) or 0

    text = "".join(b.text for b in response.content if getattr(b, "type", "") == "text").strip()
    if not text:
        raise _empty_response_error()
    outcome.text = text
    outcome.input_tokens, outcome.output_tokens = in_tok or None, out_tok or None
    return outcome


# ---------------- OpenAI 계열 (gabia / openai / azure) ----------------

def _make_openai_client(provider: str, key: str, extra: dict):
    if provider == "gabia":
        return AsyncOpenAI(api_key=key, base_url=f"{settings.ai_hub_base_url}/v1", timeout=REQUEST_TIMEOUT)
    if provider == "azure":
        endpoint = extra.get("endpoint") or ""
        if not endpoint:
            raise HTTPException(status_code=500, detail="Azure 키에 endpoint 설정이 필요합니다 (예: https://내리소스.openai.azure.com).")
        return AsyncAzureOpenAI(
            api_key=key, azure_endpoint=endpoint,
            api_version=extra.get("api_version") or "2024-10-21", timeout=REQUEST_TIMEOUT,
        )
    return AsyncOpenAI(api_key=key, timeout=REQUEST_TIMEOUT)


async def _openai_create(client, model_id: str, messages: list, tools: list | None):
    """최신 파라미터(max_completion_tokens) 우선, 미지원 프록시는 max_tokens로 재시도."""
    kwargs = {"model": model_id, "messages": messages}
    if tools:
        kwargs["tools"] = tools
    try:
        return await client.chat.completions.create(max_completion_tokens=MAX_TOKENS, **kwargs)
    except openai.BadRequestError as e:
        if "max_completion_tokens" not in str(e):
            raise
        return await client.chat.completions.create(max_tokens=MAX_TOKENS, **kwargs)


async def _run_openai(provider: str, model_id: str, key: str, extra: dict,
                      system_prompt: str | None, messages: list,
                      tool_ctx: ToolContext | None) -> ChatOutcome:
    client = _make_openai_client(provider, key, extra)
    full = ([{"role": "system", "content": system_prompt}] if system_prompt else []) + list(messages)
    tools = tool_ctx.openai_tools if tool_ctx and tool_ctx.openai_tools else None

    outcome = ChatOutcome(text="")
    in_tok = out_tok = 0

    response = await _openai_create(client, model_id, full, tools)
    rounds = 0
    while tools and rounds < MAX_TOOL_ROUNDS:
        choice = response.choices[0] if response.choices else None
        message = choice.message if choice else None
        tool_calls = getattr(message, "tool_calls", None) if message else None
        if not tool_calls:
            break
        rounds += 1
        usage = getattr(response, "usage", None)
        in_tok += getattr(usage, "prompt_tokens", 0) or 0
        out_tok += getattr(usage, "completion_tokens", 0) or 0

        full.append(message.model_dump(exclude_none=True))
        for tc in tool_calls:
            name = tc.function.name
            try:
                params = json.loads(tc.function.arguments or "{}")
            except ValueError:
                params = {}
            try:
                result_text = await tool_ctx.executor(name, params)
                outcome.used_skills.append(SkillCall(name=name, title=tool_ctx.titles.get(name, name), status="success"))
            except Exception as e:
                result_text = f"오류: {str(e)[:300]}"
                outcome.used_skills.append(SkillCall(name=name, title=tool_ctx.titles.get(name, name),
                                                     status="error", detail=str(e)[:200]))
            full.append({"role": "tool", "tool_call_id": tc.id, "content": result_text[:20000]})
        response = await _openai_create(client, model_id, full, tools)

    choice = response.choices[0] if response.choices else None
    message = choice.message if choice else None
    text = (message.content or "").strip() if message else ""
    if not text:
        refusal = getattr(message, "refusal", None) if message else None
        if refusal:
            raise HTTPException(status_code=502, detail=f"모델이 응답을 거부했습니다: {refusal}")
        raise _empty_response_error()
    usage = getattr(response, "usage", None)
    in_tok += getattr(usage, "prompt_tokens", 0) or 0
    out_tok += getattr(usage, "completion_tokens", 0) or 0

    outcome.text = text
    outcome.input_tokens, outcome.output_tokens = in_tok or None, out_tok or None
    return outcome


# ---------------- Google Gemini (REST) ----------------

def _to_plain_text(content) -> str:
    """멀티모달 content(리스트)를 텍스트로 평탄화 (Gemini 경로는 텍스트 전용)."""
    if isinstance(content, str):
        return content
    parts = []
    for block in content or []:
        if isinstance(block, dict):
            if block.get("type") == "text":
                parts.append(block.get("text", ""))
            elif block.get("type") == "image_url":
                parts.append("[이미지 첨부]")
            elif block.get("type") in ("image", "document"):
                parts.append("[파일 첨부]")
    return "\n".join(p for p in parts if p)


async def _run_gemini(model_id: str, key: str, system_prompt: str | None, messages: list) -> ChatOutcome:
    contents = []
    for m in messages:
        role = "model" if m["role"] == "assistant" else "user"
        contents.append({"role": role, "parts": [{"text": _to_plain_text(m["content"]) or " "}]})
    payload = {"contents": contents, "generationConfig": {"maxOutputTokens": MAX_TOKENS}}
    if system_prompt:
        payload["systemInstruction"] = {"parts": [{"text": system_prompt}]}

    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_id}:generateContent"
    # API 키는 쿼리스트링 대신 헤더로 전송(프록시/접근 로그에 키 노출 방지)
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
        r = await client.post(url, headers={"x-goog-api-key": key}, json=payload)
        r.raise_for_status()
        data = r.json()

    candidates = data.get("candidates") or []
    parts = (candidates[0].get("content") or {}).get("parts") if candidates else None
    text = "".join(p.get("text", "") for p in (parts or [])).strip()
    if not text:
        raise _empty_response_error()
    usage = data.get("usageMetadata") or {}
    return ChatOutcome(
        text=text,
        input_tokens=usage.get("promptTokenCount"),
        output_tokens=usage.get("candidatesTokenCount"),
    )


# ---------------- 공개 진입점 ----------------

async def run_chat(db: AsyncSession, provider: str, model_id: str,
                   system_prompt: str | None, messages: list,
                   tool_ctx: ToolContext | None) -> ChatOutcome:
    """messages는 프로바이더 계열에 맞는 형식으로 전달한다
    (ANTHROPIC_FAMILY → anthropic 블록 형식, 그 외 → OpenAI chat 형식)."""
    key, extra = await resolve_credentials(db, provider)

    if provider in ANTHROPIC_FAMILY:
        return await _run_anthropic(provider, model_id, key, extra, system_prompt, messages, tool_ctx)
    if provider in OPENAI_FAMILY:
        return await _run_openai(provider, model_id, key, extra, system_prompt, messages, tool_ctx)
    if provider == "gemini":
        return await _run_gemini(model_id, key, system_prompt, messages)

    raise HTTPException(status_code=500, detail=f"알 수 없는 프로바이더: {provider}")


# ---------------- 스트리밍 (SSE) ----------------
# run_chat_stream은 async generator로 이벤트 튜플을 yield 한다:
#   ("delta", str)          — 실시간 텍스트 조각
#   ("tool", title)         — 도구 실행 시작
#   ("skill", SkillCall)    — 도구 실행 결과(성공/실패)
#   ("done", ChatOutcome)   — 최종 결과(누적 텍스트+토큰+used_skills)

async def _stream_anthropic(provider, model_id, key, extra, system_prompt, messages, tool_ctx):
    client = _make_anthropic_client(provider, key, extra)
    kwargs = {"model": model_id, "max_tokens": MAX_TOKENS, "messages": list(messages)}
    if system_prompt:
        kwargs["system"] = system_prompt
    if tool_ctx and tool_ctx.anthropic_tools:
        kwargs["tools"] = tool_ctx.anthropic_tools

    outcome = ChatOutcome(text="")
    streamed: list[str] = []
    in_tok = out_tok = 0
    rounds = 0
    while True:
        async with client.messages.stream(**kwargs) as stream:
            async for event in stream:
                if getattr(event, "type", "") == "content_block_delta" and \
                        getattr(event.delta, "type", "") == "text_delta":
                    chunk = event.delta.text
                    streamed.append(chunk)
                    yield ("delta", chunk)
            final = await stream.get_final_message()
        usage = getattr(final, "usage", None)
        in_tok += getattr(usage, "input_tokens", 0) or 0
        out_tok += getattr(usage, "output_tokens", 0) or 0
        if getattr(final, "stop_reason", None) == "tool_use" and tool_ctx and rounds < MAX_TOOL_ROUNDS:
            rounds += 1
            kwargs["messages"].append({"role": "assistant", "content": _anthropic_assistant_content(final.content)})
            tool_results = []
            for block in final.content:
                if getattr(block, "type", "") != "tool_use":
                    continue
                name = block.name
                yield ("tool", tool_ctx.titles.get(name, name))
                try:
                    result_text = await tool_ctx.executor(name, dict(block.input or {}))
                    sc = SkillCall(name=name, title=tool_ctx.titles.get(name, name), status="success")
                except Exception as e:
                    result_text = f"오류: {str(e)[:300]}"
                    sc = SkillCall(name=name, title=tool_ctx.titles.get(name, name), status="error", detail=str(e)[:200])
                outcome.used_skills.append(sc)
                yield ("skill", sc)
                tool_results.append({"type": "tool_result", "tool_use_id": block.id, "content": result_text[:20000]})
            kwargs["messages"].append({"role": "user", "content": tool_results})
            continue
        break
    outcome.text = "".join(streamed).strip()
    outcome.input_tokens, outcome.output_tokens = in_tok or None, out_tok or None
    if not outcome.text and not outcome.used_skills:
        raise _empty_response_error()
    yield ("done", outcome)


async def _openai_stream_create(client, model_id: str, messages: list, tools: list | None):
    base = {"model": model_id, "messages": messages, "stream": True}
    if tools:
        base["tools"] = tools
    attempts = (
        {"max_completion_tokens": MAX_TOKENS, "stream_options": {"include_usage": True}},
        {"max_tokens": MAX_TOKENS, "stream_options": {"include_usage": True}},
        {"max_tokens": MAX_TOKENS},
    )
    last = None
    for extra_kw in attempts:
        try:
            return await client.chat.completions.create(**base, **extra_kw)
        except openai.BadRequestError as e:
            last = e
            continue
    raise last


async def _stream_openai(provider, model_id, key, extra, system_prompt, messages, tool_ctx):
    client = _make_openai_client(provider, key, extra)
    full = ([{"role": "system", "content": system_prompt}] if system_prompt else []) + list(messages)
    tools = tool_ctx.openai_tools if tool_ctx and tool_ctx.openai_tools else None

    outcome = ChatOutcome(text="")
    streamed: list[str] = []
    in_tok = out_tok = 0
    rounds = 0
    while True:
        content_parts: list[str] = []
        tool_acc: dict = {}   # index → {id, name, args}
        finish = None
        stream = await _openai_stream_create(client, model_id, full, tools)
        async for chunk in stream:
            usage = getattr(chunk, "usage", None)
            if usage:
                in_tok += getattr(usage, "prompt_tokens", 0) or 0
                out_tok += getattr(usage, "completion_tokens", 0) or 0
            choices = getattr(chunk, "choices", None)
            if not choices:
                continue
            ch = choices[0]
            delta = getattr(ch, "delta", None)
            if delta is not None:
                if getattr(delta, "content", None):
                    content_parts.append(delta.content)
                    streamed.append(delta.content)
                    yield ("delta", delta.content)
                for tc in (getattr(delta, "tool_calls", None) or []):
                    slot = tool_acc.setdefault(tc.index, {"id": None, "name": "", "args": ""})
                    if getattr(tc, "id", None):
                        slot["id"] = tc.id
                    fn = getattr(tc, "function", None)
                    if fn:
                        if getattr(fn, "name", None):
                            slot["name"] = fn.name
                        if getattr(fn, "arguments", None):
                            slot["args"] += fn.arguments
            if getattr(ch, "finish_reason", None):
                finish = ch.finish_reason
        if finish == "tool_calls" and tools and tool_acc and rounds < MAX_TOOL_ROUNDS:
            rounds += 1
            full.append({
                "role": "assistant",
                "content": "".join(content_parts) or None,
                "tool_calls": [{"id": s["id"], "type": "function",
                                "function": {"name": s["name"], "arguments": s["args"] or "{}"}}
                               for s in tool_acc.values()],
            })
            for s in tool_acc.values():
                name = s["name"]
                yield ("tool", tool_ctx.titles.get(name, name))
                try:
                    params = json.loads(s["args"] or "{}")
                except ValueError:
                    params = {}
                try:
                    result_text = await tool_ctx.executor(name, params)
                    sc = SkillCall(name=name, title=tool_ctx.titles.get(name, name), status="success")
                except Exception as e:
                    result_text = f"오류: {str(e)[:300]}"
                    sc = SkillCall(name=name, title=tool_ctx.titles.get(name, name), status="error", detail=str(e)[:200])
                outcome.used_skills.append(sc)
                yield ("skill", sc)
                full.append({"role": "tool", "tool_call_id": s["id"], "content": result_text[:20000]})
            continue
        break
    outcome.text = "".join(streamed).strip()
    outcome.input_tokens, outcome.output_tokens = in_tok or None, out_tok or None
    if not outcome.text and not outcome.used_skills:
        raise _empty_response_error()
    yield ("done", outcome)


async def run_chat_stream(db: AsyncSession, provider: str, model_id: str,
                          system_prompt: str | None, messages: list,
                          tool_ctx: ToolContext | None):
    """run_chat의 스트리밍 버전. 이벤트 튜플을 async yield 한다."""
    key, extra = await resolve_credentials(db, provider)
    if provider in ANTHROPIC_FAMILY:
        async for ev in _stream_anthropic(provider, model_id, key, extra, system_prompt, messages, tool_ctx):
            yield ev
    elif provider in OPENAI_FAMILY:
        async for ev in _stream_openai(provider, model_id, key, extra, system_prompt, messages, tool_ctx):
            yield ev
    elif provider == "gemini":
        # Gemini REST 경로는 스트리밍 미지원 → 전체 텍스트를 한 번에 델타로 흘려보냄
        outcome = await _run_gemini(model_id, key, system_prompt, messages)
        if outcome.text:
            yield ("delta", outcome.text)
        yield ("done", outcome)
    else:
        raise HTTPException(status_code=500, detail=f"알 수 없는 프로바이더: {provider}")


# ---------------- 이미지 생성 ----------------

@dataclass
class GeneratedImage:
    data: bytes
    content_type: str = "image/png"


def _image_base_url(provider: str, extra: dict) -> str:
    if provider == "gabia":
        return f"{settings.ai_hub_base_url}/v1"
    if provider == "azure":
        endpoint = (extra.get("endpoint") or "").rstrip("/")
        if not endpoint:
            raise HTTPException(status_code=500, detail="Azure 키에 endpoint 설정이 필요합니다.")
        api_version = extra.get("api_version") or "2024-10-21"
        # Azure 이미지: deployment 경로. model_id를 deployment 이름으로 사용.
        return f"{endpoint}/openai/deployments/{{model}}/images/generations?api-version={api_version}"
    return "https://api.openai.com/v1"  # openai 공식


async def _run_openai_image(provider: str, model_id: str, key: str, extra: dict,
                            prompt: str, size: str) -> list[GeneratedImage]:
    payload = {"prompt": prompt[:4000], "n": 1, "size": size, "response_format": "b64_json"}
    if provider == "azure":
        url = _image_base_url(provider, extra).format(model=model_id)
        headers = {"api-key": key}
    else:
        base = _image_base_url(provider, extra)
        url = f"{base}/images/generations"
        headers = {"Authorization": f"Bearer {key}"}
        payload["model"] = model_id
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
        r = await client.post(url, headers=headers, json=payload)
        if r.status_code >= 400:
            # 일부 모델은 response_format 미지원 → url 방식으로 재시도
            if "response_format" in r.text:
                payload.pop("response_format", None)
                r = await client.post(url, headers=headers, json=payload)
        if r.status_code in (404, 405):
            # 엔드포인트 자체가 없음 → 이 프로바이더는 이미지 생성 미지원(예: 가비아 AI Hub)
            raise HTTPException(
                status_code=502,
                detail="이 프로바이더는 이미지 생성 API를 제공하지 않습니다. "
                       "관리자 패널에서 OpenAI 또는 Gemini 이미지 모델을 등록해 사용하세요.",
            )
        r.raise_for_status()
        data = r.json()
    out = []
    for item in (data.get("data") or []):
        if item.get("b64_json"):
            out.append(GeneratedImage(base64.standard_b64decode(item["b64_json"])))
        elif item.get("url"):
            async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
                img = await client.get(item["url"])
                img.raise_for_status()
                out.append(GeneratedImage(img.content, img.headers.get("content-type", "image/png")))
    if not out:
        raise HTTPException(status_code=502, detail="이미지 응답을 받지 못했습니다.")
    return out


async def _run_gemini_image(model_id: str, key: str, prompt: str) -> list[GeneratedImage]:
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_id}:generateContent"
    payload = {"contents": [{"role": "user", "parts": [{"text": prompt[:4000]}]}],
               "generationConfig": {"responseModalities": ["IMAGE", "TEXT"]}}
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
        r = await client.post(url, headers={"x-goog-api-key": key}, json=payload)
        r.raise_for_status()
        data = r.json()
    out = []
    for cand in (data.get("candidates") or []):
        for part in ((cand.get("content") or {}).get("parts") or []):
            inline = part.get("inlineData") or part.get("inline_data")
            if inline and inline.get("data"):
                out.append(GeneratedImage(base64.standard_b64decode(inline["data"]),
                                          inline.get("mimeType") or inline.get("mime_type") or "image/png"))
    if not out:
        raise HTTPException(status_code=502, detail="이미지 응답을 받지 못했습니다.")
    return out


async def run_image(db: AsyncSession, provider: str, model_id: str, prompt: str,
                    size: str = "1024x1024") -> list[GeneratedImage]:
    if provider not in IMAGE_PROVIDERS:
        raise HTTPException(status_code=400, detail=f"{provider}는 이미지 생성을 지원하지 않습니다.")
    key, extra = await resolve_credentials(db, provider)
    if provider == "gemini":
        images = await _run_gemini_image(model_id, key, prompt)
    else:
        images = await _run_openai_image(provider, model_id, key, extra, prompt, size)
    for img in images:
        if len(img.data) > MAX_IMAGE_OUT_BYTES:
            raise HTTPException(status_code=502, detail="생성 이미지가 너무 큽니다.")
    return images
