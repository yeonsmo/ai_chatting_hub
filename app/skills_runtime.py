"""스킬 실행 런타임.

스킬 = 연동(Integration)의 인증으로 호출하는 HTTP API 한 개.
경로/쿼리/바디의 {param} 자리표시자를 모델이 준 인자로 치환해 호출하고,
결과 텍스트(JSON이면 정리된 형태)를 모델에게 돌려준다.
"""
import ipaddress
import json
import re
import socket
from urllib.parse import urlparse

import httpx
from app.models import Integration, Skill

RESULT_MAX_CHARS = 20_000
PARAM_RE = re.compile(r"\{(\w+)\}")

ALLOWED_TYPES = {"string", "number", "integer", "boolean"}


def parse_params_schema(skill: Skill) -> list[dict]:
    try:
        raw = json.loads(skill.params_schema) if skill.params_schema else []
    except (ValueError, TypeError):
        raw = []
    out = []
    for p in raw if isinstance(raw, list) else []:
        if not isinstance(p, dict) or not p.get("name"):
            continue
        out.append({
            "name": str(p["name"])[:60],
            "type": p.get("type") if p.get("type") in ALLOWED_TYPES else "string",
            "description": str(p.get("description", ""))[:300],
            "required": bool(p.get("required", False)),
        })
    return out


def to_json_schema(skill: Skill) -> dict:
    """anthropic input_schema / openai parameters 공용 JSON Schema."""
    props, required = {}, []
    for p in parse_params_schema(skill):
        props[p["name"]] = {"type": p["type"], "description": p["description"]}
        if p["required"]:
            required.append(p["name"])
    schema = {"type": "object", "properties": props}
    if required:
        schema["required"] = required
    return schema


def anthropic_tool_def(skill: Skill) -> dict:
    return {"name": skill.name, "description": skill.description[:1000], "input_schema": to_json_schema(skill)}


def openai_tool_def(skill: Skill) -> dict:
    return {"type": "function", "function": {
        "name": skill.name, "description": skill.description[:1000], "parameters": to_json_schema(skill),
    }}


def _substitute(template: str, params: dict) -> str:
    def repl(m):
        key = m.group(1)
        if key not in params:
            raise ValueError(f"필수 파라미터 누락: {key}")
        return str(params[key])
    return PARAM_RE.sub(repl, template)


def _check_url_safety(url: str):
    """클라우드 메타데이터 등 명백히 위험한 대상 차단."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError("http/https URL만 허용됩니다")
    host = parsed.hostname or ""
    try:
        infos = socket.getaddrinfo(host, None)
        for info in infos:
            ip = ipaddress.ip_address(info[4][0])
            if ip in ipaddress.ip_network("169.254.0.0/16"):
                raise ValueError("차단된 대상입니다")
    except socket.gaierror:
        pass  # DNS 실패는 호출 단계에서 에러로 드러남


def _build_request(skill: Skill, integration: Integration, params: dict):
    url = integration.base_url.rstrip("/") + "/" + _substitute(skill.path, params).lstrip("/")
    _check_url_safety(url)

    headers = {"Accept": "application/json"}
    query: dict = {}

    if integration.auth_type == "bearer" and integration.credential:
        headers["Authorization"] = f"Bearer {integration.credential}"
    elif integration.auth_type == "header" and integration.credential:
        headers[integration.auth_key or "X-API-Key"] = integration.credential
    elif integration.auth_type == "query" and integration.credential:
        query[integration.auth_key or "api_key"] = integration.credential

    if integration.extra_headers:
        try:
            for k, v in (json.loads(integration.extra_headers) or {}).items():
                headers[str(k)[:100]] = str(v)[:500]
        except (ValueError, TypeError, AttributeError):
            pass

    if skill.query_template:
        try:
            for k, v in (json.loads(skill.query_template) or {}).items():
                if isinstance(v, str):
                    try:
                        query[k] = _substitute(v, params)
                    except ValueError:
                        continue  # 선택 파라미터가 없으면 해당 쿼리 생략
                else:
                    query[k] = v
        except (ValueError, TypeError, AttributeError):
            pass

    body = None
    if skill.body_template and skill.method.upper() in ("POST", "PUT", "PATCH"):
        body = _substitute(skill.body_template, params)

    return url, headers, query, body


async def execute_skill(skill: Skill, integration: Integration, params: dict) -> str:
    url, headers, query, body = _build_request(skill, integration, params)
    timeout = max(3, min(skill.timeout_s or 20, 60))

    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        kwargs = {"headers": headers, "params": query}
        if body is not None:
            kwargs["content"] = body.encode()
            kwargs["headers"]["Content-Type"] = "application/json"
        response = await client.request(skill.method.upper(), url, **kwargs)

    text = response.text[:RESULT_MAX_CHARS]
    try:  # JSON이면 보기 좋게 정리
        text = json.dumps(response.json(), ensure_ascii=False, indent=1)[:RESULT_MAX_CHARS]
    except ValueError:
        pass

    if response.status_code >= 400:
        return f"[HTTP {response.status_code}] {text[:2000]}"
    return text or "(빈 응답)"
