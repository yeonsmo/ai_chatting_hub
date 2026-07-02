import ipaddress
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response


class IPWhitelistMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, allowed_ips: str):
        super().__init__(app)
        self.allowed_networks = []
        for ip_str in allowed_ips.split(","):
            ip_str = ip_str.strip()
            try:
                self.allowed_networks.append(ipaddress.ip_network(ip_str, strict=False))
            except ValueError:
                try:
                    self.allowed_networks.append(ipaddress.ip_address(ip_str))
                except ValueError:
                    pass

    def is_allowed(self, ip: str) -> bool:
        try:
            client_ip = ipaddress.ip_address(ip)
            for network in self.allowed_networks:
                if isinstance(network, (ipaddress.IPv4Network, ipaddress.IPv6Network)):
                    if client_ip in network:
                        return True
                else:
                    if client_ip == network:
                        return True
        except ValueError:
            pass
        return False

    async def dispatch(self, request, call_next):
        if request.url.path == "/health":
            return await call_next(request)

        # X-Forwarded-For 우선 (Nginx 리버스 프록시 환경)
        client_ip = request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
        if not client_ip:
            client_ip = request.client.host if request.client else ""

        if not self.is_allowed(client_ip):
            return Response(content="Access denied", status_code=403)

        return await call_next(request)
