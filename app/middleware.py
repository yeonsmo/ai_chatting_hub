import ipaddress
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response


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
        # 전부 신뢰 프록시면 체인의 맨 앞이 원 클라이언트
        return hops[0] if hops else peer

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
