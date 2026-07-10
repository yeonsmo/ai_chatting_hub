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

    # 회의 녹음 전사(자체 호스팅 STT). API 키 불필요, 오디오가 서버 밖으로 나가지 않음.
    # 모델은 최초 사용 시 HuggingFace에서 1회 다운로드됨(서버 인터넷 필요) → 이후 캐시.
    # 모델: tiny/base/small/medium/large-v3 (클수록 정확·느림·메모리↑). 기본 small.
    whisper_model: str = "small"
    whisper_device: str = "cpu"          # GPU 서버면 "cuda"
    whisper_compute: str = "int8"        # cpu=int8 권장, cuda면 "float16"
    whisper_language: str = "ko"

    # 회의록 정리에 사용할 모델(라우팅 key). 요약 작업이라 Pro(느림)보다 표준 GPT가
    # 속도/품질 균형이 좋다. 없으면 자동 대체. 최상위 품질을 원하면 gpt-5-pro로 변경.
    minutes_model_key: str = "gpt-5"

    # 대화 중 "이미지 그려줘"를 자동 처리할 이미지 모델(라우팅 key). 비우면 kind=image 첫 라우팅 사용.
    image_model_key: str = ""

    # HR 모듈 연동(직원 명부 동기화). 사내망 HR 서버 REST API로 계정·부서·재직상태를 맞춘다.
    # 비우면 HR 동기화 비활성. 인증은 정적 API 키(헤더) 기본.
    hr_base_url: str = ""                       # 예: https://hr.hpengineeringwork.com
    hr_api_key: str = ""
    hr_auth_header: str = "Authorization"       # 키를 실을 헤더명(예: X-API-Key)
    hr_auth_prefix: str = "Bearer "             # 헤더 값 접두(예: "Bearer " 또는 "")
    hr_employees_path: str = "/api/v1/employees"
    hr_documents_path: str = "/api/v1/documents/ingest"   # 기안 서류 전송(결재 연동)
    hr_page_size: int = 100
    hr_auto_create: bool = False                # HR에만 있는 직원 계정을 자동 생성할지
    hr_default_role: str = "user"               # 자동 생성 시 기본 역할
    hr_role_map: str = "{}"                      # 직급/직위→역할 매핑 JSON(예: {"부장":"manager"})

    # SSO(하이웍스 등 OAuth2 로그인). 진짜 본인인증을 IdP에 위임. 비우면 비활성.
    # 하이웍스 관리자/개발자센터에서 앱 등록 후 값 채우기(리다이렉트 URI = <허브>/api/auth/sso/callback).
    sso_enabled: bool = False
    sso_label: str = "하이웍스"
    sso_authorize_url: str = ""
    sso_token_url: str = ""
    sso_userinfo_url: str = ""
    sso_client_id: str = ""
    sso_client_secret: str = ""
    sso_redirect_uri: str = ""
    sso_scope: str = ""
    sso_userinfo_email_field: str = "email"       # 하이웍스 사용자정보 응답의 이메일 키
    sso_userinfo_empno_field: str = "employee_no" # 사번 키(있으면 HR 사번과 매칭)
    sso_userinfo_name_field: str = "name"
    sso_auto_create: bool = False                 # 미등록 계정 자동 생성 여부
    sso_default_role: str = "user"

    # 스킬/연동 SSRF 예외: 신뢰하는 사내 호스트만 사설 대역이어도 허용.
    # 콤마 구분. 와일드카드(*.example.com) / 정확한 호스트 / CIDR(192.168.0.0/24) 지원.
    # 기본값은 사내 도메인만. 비우면 모든 사설 대역 차단(가장 안전).
    skill_internal_allowed_hosts: str = "*.hpengineeringwork.com"

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
