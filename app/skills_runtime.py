"""스킬 실행 런타임.

스킬 = 연동(Integration)의 인증으로 호출하는 HTTP API 한 개.
경로/쿼리/바디의 {param} 자리표시자를 모델이 준 인자로 치환해 호출하고,
결과 텍스트(JSON이면 정리된 형태)를 모델에게 돌려준다.
"""
import ipaddress
import json
import re
import socket
from urllib.parse import urlparse, quote

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


def _substitute_query(template: str, params: dict) -> str:
    """쿼리 값 치환 — httpx params=가 별도로 URL 인코딩하므로 원값 문자열 반환."""
    def repl(m):
        key = m.group(1)
        if key not in params:
            raise ValueError(f"필수 파라미터 누락: {key}")
        return str(params[key])
    return PARAM_RE.sub(repl, template)


def _substitute_path(template: str, params: dict) -> str:
    """경로 치환 — 주입/트래버설 방지를 위해 치환 값은 퍼센트 인코딩(slash 포함)."""
    def repl(m):
        key = m.group(1)
        if key not in params:
            raise ValueError(f"필수 파라미터 누락: {key}")
        return quote(str(params[key]), safe="")
    return PARAM_RE.sub(repl, template)


def _render_json_leaf(value, params):
    if isinstance(value, str):
        # 문자열 전체가 정확히 하나의 {param}이면 원래 타입(숫자/불리언 등) 유지
        full = PARAM_RE.fullmatch(value)
        if full:
            key = full.group(1)
            if key not in params:
                raise ValueError(f"필수 파라미터 누락: {key}")
            return params[key]
        return _substitute_query(value, params)  # 문자열 내 삽입 → 값 문자열화
    if isinstance(value, dict):
        return {k: _render_json_leaf(v, params) for k, v in value.items()}
    if isinstance(value, list):
        return [_render_json_leaf(v, params) for v in value]
    return value


def _render_json_body(template: str, params: dict) -> str:
    """body_template을 JSON 구조로 파싱한 뒤 값 위치에만 치환 → json.dumps로 재직렬화.
    이렇게 하면 파라미터 값의 따옴표/중괄호가 이스케이프되어 JSON 인젝션이 불가능하다."""
    obj = json.loads(template)  # 관리자가 등록한 템플릿은 유효 JSON이어야 함
    rendered = _render_json_leaf(obj, params)
    return json.dumps(rendered, ensure_ascii=False)


def _validate_public_host(host: str):
    """호스트를 해석해 loopback/사설/링크로컬/예약 대역이면 차단하고, 안전한 IP 목록을 반환."""
    if not host:
        raise ValueError("호스트가 비어 있습니다")
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        raise ValueError("호스트를 해석할 수 없습니다")
    ips = []
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved
                or ip.is_multicast or ip.is_unspecified
                or (ip.version == 6 and ip.ipv4_mapped and (
                    ip.ipv4_mapped.is_private or ip.ipv4_mapped.is_loopback
                    or ip.ipv4_mapped.is_link_local))):
            raise ValueError("내부/사설 대역 대상은 호출할 수 없습니다")
        ips.append(str(ip))
    if not ips:
        raise ValueError("호스트를 해석할 수 없습니다")
    return ips


def _build_request(skill: Skill, integration: Integration, params: dict):
    url = integration.base_url.rstrip("/") + "/" + _substitute_path(skill.path, params).lstrip("/")
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError("http/https URL만 허용됩니다")
    _validate_public_host(parsed.hostname or "")

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
                        query[k] = _substitute_query(v, params)
                    except ValueError:
                        continue  # 선택 파라미터가 없으면 해당 쿼리 생략
                else:
                    query[k] = v
        except (ValueError, TypeError, AttributeError):
            pass

    body = None
    if skill.body_template and skill.method.upper() in ("POST", "PUT", "PATCH"):
        body = _render_json_body(skill.body_template, params)

    return url, headers, query, body


async def execute_skill(skill: Skill, integration: Integration, params: dict) -> str:
    url, headers, query, body = _build_request(skill, integration, params)
    timeout = max(3, min(skill.timeout_s or 20, 60))

    # follow_redirects=False: 리다이렉트로 사설/메타데이터 대역에 도달하는 SSRF 우회를 차단.
    # 리다이렉트를 최대 3회까지 직접 따라가되 매 홉마다 대상 호스트를 재검증한다.
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
        method = skill.method.upper()
        current_url, current_method = url, method
        response = None
        for _ in range(4):
            kwargs = {"headers": dict(headers), "params": query}
            if body is not None and current_method in ("POST", "PUT", "PATCH"):
                kwargs["content"] = body.encode()
                kwargs["headers"]["Content-Type"] = "application/json"
            response = await client.request(current_method, current_url, **kwargs)
            if response.status_code not in (301, 302, 303, 307, 308):
                break
            location = response.headers.get("location")
            if not location:
                break
            current_url = str(response.url.join(location))
            p = urlparse(current_url)
            if p.scheme not in ("http", "https"):
                raise ValueError("허용되지 않은 리다이렉트 대상입니다")
            _validate_public_host(p.hostname or "")  # 매 홉 재검증(사설/메타데이터 차단)
            query = {}  # 리다이렉트 이후에는 원 쿼리를 재부착하지 않음
            if response.status_code == 303:
                current_method, body = "GET", None
        else:
            raise ValueError("리다이렉트가 너무 많습니다")

    text = response.text[:RESULT_MAX_CHARS]
    try:  # JSON이면 보기 좋게 정리
        text = json.dumps(response.json(), ensure_ascii=False, indent=1)[:RESULT_MAX_CHARS]
    except ValueError:
        pass

    if response.status_code >= 400:
        return f"[HTTP {response.status_code}] {text[:2000]}"
    return text or "(빈 응답)"
