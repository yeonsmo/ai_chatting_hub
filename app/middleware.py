import ipaddress
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response

# 콘텐츠 보안 정책(CSP). 인라인 스크립트/스타일/onclick을 많이 쓰므로 script/style에
# 'unsafe-inline'이 불가피하지만, connect-src/img-src를 'self'로 제한해 토큰 탈취 시
# 외부로의 유출 경로(fetch/XHR/이미지 비콘)를 차단한다(방어층). 폰트는 CDN 허용.
_CSP = (
    "default-src 'self'; "
    "script-src 'self' 'unsafe-inline'; "
    "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net https://fonts.googleapis.com; "
    "font-src 'self' https://cdn.jsdelivr.net https://fonts.gstatic.com; "
    "img-src 'self' data:; "
    "connect-src 'self'; "
    "object-src 'none'; base-uri 'self'; frame-ancestors 'none'; form-action 'self'"
)


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """모든 응답에 보안 헤더 부여(CSP, 클릭재킹/스니핑 방지 등)."""

    async def dispatch(self, request, call_next):
        response = await call_next(request)
        response.headers.setdefault("Content-Security-Policy", _CSP)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault("Cross-Origin-Opener-Policy", "same-origin")
        return response


class MaxBodySizeMiddleware:
    """요청 본문 크기 상한(순수 ASGI). Content-Length 사전 거부 + 스트리밍 바이트
    카운팅 백스톱으로, 멀티파트가 디스크 스풀에 통째로 기록되기 전에 과대 요청을 차단."""

    def __init__(self, app, max_body_bytes: int):
        self.app = app
        self.max_body_bytes = max_body_bytes

    async def _reject(self, send):
        await send({"type": "http.response.start", "status": 413,
                    "headers": [(b"content-type", b"text/plain; charset=utf-8")]})
        await send({"type": "http.response.body", "body": "요청 본문이 너무 큽니다".encode()})

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        for name, value in scope.get("headers") or []:
            if name == b"content-length":
                try:
                    if int(value) > self.max_body_bytes:
                        return await self._reject(send)
                except ValueError:
                    pass
                break

        total = 0
        async def rcv():
            nonlocal total
            message = await receive()
            if message.get("type") == "http.request":
                total += len(message.get("body", b""))
                if total > self.max_body_bytes:
                    # 상한 초과 시 클라이언트 연결 종료로 파서를 중단(디스크 스풀 폭증 방지)
                    return {"type": "http.disconnect"}
            return message

        await self.app(scope, rcv, send)


def _parse_networks(csv: str):
    networks = []
    for ip_str in csv.split(","):
        ip_str = ip_str.strip()
        if not ip_str:
            continue
        try:
            networks.append(ipaddress.ip_network(ip_str, strict=False))
        except ValueError:
            pass
    return networks


def _in_networks(ip: str, networks) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return any(addr in net for net in networks)


class IPWhitelistMiddleware(BaseHTTPMiddleware):
    """IP 화이트리스트.

    X-Forwarded-For는 클라이언트가 임의로 붙일 수 있는 헤더이므로,
    직접 접속한 피어(request.client.host)가 신뢰 프록시 목록에 있을 때만 사용한다.
    XFF 체인은 오른쪽(프록시에 가까운 쪽)에서 왼쪽으로 훑으며
    신뢰 프록시가 아닌 첫 IP를 실제 클라이언트로 본다.
    """

    def __init__(self, app, allowed_ips: str, trusted_proxies: str = "127.0.0.1"):
        super().__init__(app)
        self.allowed_networks = _parse_networks(allowed_ips)
        self.trusted_networks = _parse_networks(trusted_proxies)

    def resolve_client_ip(self, request) -> str:
        peer = request.client.host if request.client else ""
        if not peer or not _in_networks(peer, self.trusted_networks):
            return peer

        xff = request.headers.get("X-Forwarded-For", "")
        if not xff:
            return peer

        hops = [h.strip() for h in xff.split(",") if h.strip()]
        for hop in reversed(hops):
            if not _in_networks(hop, self.trusted_networks):
                return hop
        # 체인이 전부 신뢰 프록시면 비신뢰 클라이언트가 없다는 뜻.
        # hops[0]은 클라이언트가 위조할 수 있으므로 신뢰하지 않고, 위조 불가한
        # 실제 TCP 피어를 반환한다(화이트리스트 우회 방지).
        return peer

    def is_allowed(self, ip: str) -> bool:
        return _in_networks(ip, self.allowed_networks)

    async def dispatch(self, request, call_next):
        if request.url.path == "/health":
            return await call_next(request)

        client_ip = self.resolve_client_ip(request)
        request.state.client_ip = client_ip  # 사용 로그에서 참조
        if not self.is_allowed(client_ip):
            return Response(content="Access denied", status_code=403)

        return await call_next(request)
