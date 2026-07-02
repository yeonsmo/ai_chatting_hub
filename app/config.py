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
    # X-Forwarded-For를 신뢰할 프록시(직접 접속 IP가 여기 속할 때만 XFF 사용)
    trusted_proxies: str = "127.0.0.1,::1,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16"

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
