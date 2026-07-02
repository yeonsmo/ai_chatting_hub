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

    anthropic_api_key: str = ""
    gabia_api_key: str = ""
    ai_hub_base_url: str = "https://ai-hub.gabia.com"

    superadmin_username: str = "superadmin"
    superadmin_initial_password: str = ""

    @property
    def database_url(self) -> str:
        return f"postgresql+asyncpg://{self.db_user}:{self.db_password}@{self.db_host}:{self.db_port}/{self.db_name}"

    class Config:
        env_file = ".env"


settings = Settings()
