from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    db_host: str = "postgres"
    db_port: int = 5432
    db_user: str = "n8n"
    db_password: str = ""
    db_name: str = "claude_chat"

    secret_key: str = ""
    algorithm: str = "HS256"
    access_token_expire_minutes: int = 480

    allowed_ips: str = "192.168.219.0/24,127.0.0.1"
    # X-Forwarded-For를 신뢰할 프록시(직접 접속 IP가 여기 속할 때만 XFF 사용).
    # 기본값은 리버스 프록시가 접속해 오는 루프백만 신뢰한다.
    # ⚠ 이 대역이 ALLOWED_IPS를 포함(상위집합)하면 XFF 위조로 화이트리스트를
    #    우회당할 수 있으므로, 실제 프록시 주소로 최소화해서 설정하세요.
    trusted_proxies: str = "127.0.0.1,::1"

    anthropic_api_key: str = ""
    gabia_api_key: str = ""
    ai_hub_base_url: str = "https://ai-hub.gabia.com"

    superadmin_username: str = "superadmin"
    superadmin_initial_password: str = ""

    # 파일 업로드
    upload_dir: str = "data/uploads"
    max_upload_mb: int = 50

    @property
    def database_url(self) -> str:
        return f"postgresql+asyncpg://{self.db_user}:{self.db_password}@{self.db_host}:{self.db_port}/{self.db_name}"

    class Config:
        env_file = ".env"


settings = Settings()

# 빈 SECRET_KEY로 뜨면 누구나 JWT를 위조할 수 있으므로 기동 자체를 막는다
if not settings.secret_key or len(settings.secret_key) < 16:
    raise RuntimeError(
        "SECRET_KEY가 설정되지 않았거나 너무 짧습니다(16자 이상). "
        ".env에 SECRET_KEY를 설정한 뒤 다시 시작하세요."
    )
