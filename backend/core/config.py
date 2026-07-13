from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # App
    APP_NAME: str = "ThinkSync"
    APP_VERSION: str = "1.28.1"
    DEBUG: bool = False

    # API
    API_HOST: str = "0.0.0.0"
    API_PORT: int = 8000

    # Supabase — used ONLY as PostgreSQL (Supabase Auth is disabled; identities
    # live in public.users). Never expose the service-role key to the client.
    SUPABASE_URL: str
    SUPABASE_ANON_KEY: str
    SUPABASE_SERVICE_ROLE_KEY: str

    # Google OAuth (the only authentication method as of the 2026-07 OAuth
    # migration). GOOGLE_CLIENT_ID must be supplied via the environment only —
    # never hard-coded. The backend verifies the Google ID token itself; no
    # Supabase/Google client secret is needed for the token-exchange flow.
    GOOGLE_CLIENT_ID: str = ""
    # Acceptable token issuers (Google's OIDC issuer + the Firebase/securetoken
    # issuer used by some mobile flows).
    GOOGLE_ISSUERS: list[str] = [
        "https://accounts.google.com",
        "https://securetoken.google.com",
    ]
    GOOGLE_CERTS_URL: str = "https://www.googleapis.com/oauth2/v3/certs"
    
    # JWT
    JWT_SECRET: str
    JWT_ALGORITHM: str = "HS256"
    JWT_EXPIRE_MINUTES: int = 60 * 24  # 24 hours

    # Optional dedicated encryption key for sensitive data at rest.
    # Must be a Fernet key if provided.
    DATA_ENCRYPTION_KEY: str | None = None

    # CORS — comma-separated list in .env, e.g. "http://localhost:3000,https://app.thinksync.art"
    # Production origins must be set explicitly via the CORS_ORIGINS env variable.
    CORS_ORIGINS: list[str] = ["http://localhost:3000"]

    # SSH
    SSH_TIMEOUT: int = 30
    SSH_COMMAND_TIMEOUT: int = 60
    # Enforce SSH host key verification in production.
    SSH_STRICT_HOST_KEY_CHECKING: bool = True
    # Path to known_hosts file used when strict checking is enabled.
    SSH_KNOWN_HOSTS: str = "~/.ssh/known_hosts"

    # Agent (Forge v1)
    AGENT_ADMIN_EMAILS: str = ""
    AGENT_ALLOWED_COMMAND_PREFIXES: str = (
        "uname,uptime,whoami,id,pwd,ls,df,free,cat,head,tail,ps,ss,netstat,"
        "docker ps,docker images,systemctl status,journalctl"
    )
    AGENT_STEP_TIMEOUT: int = 45
    AGENT_MAX_CONCURRENCY: int = 2
    AGENT_AUDIT_LOGGING_ENABLED: bool = True
    AGENT_AUDIT_TABLE: str = "agent_runs"

    # Agent (Forge v2)
    AGENT_MAX_RETRIES: int = 3
    AGENT_V2_WRITE_TOOLS: str = "restart_service,deploy_app"
    # Global write gate: set to False to disable all write actions across the pipeline.
    # Individual allow_write flags are still checked, but this is the single
    # production kill-switch that overrides everything.
    AGENT_ALLOW_WRITE: bool = True

    # Sprint 3: Approval subsystem
    # MUST be set in production. Fail fast at startup if missing.
    APPROVAL_RESUME_SECRET: str = ""

    # Sprint 3C.C: Event-Driven Wait Engine
    # Configurable wait window for a suspended job.  Default 30 minutes;
    # configurable between 30 and 60 minutes (1800-3600 seconds).  Any value
    # outside the range is clamped to the nearest bound at access time.
    WAIT_TIMEOUT_SECONDS: int = 1800

    # Redis (Upstash) — optional; used by Forge v2 for LLM response caching
    REDIS_URL: str | None = None
    REDIS_CHAT_MEMORY_TTL_SECONDS: int = 60 * 60 * 24
    REDIS_CHAT_MEMORY_MAX_ITEMS: int = 50
    REDIS_JOB_EVENT_TTL_SECONDS: int = 60 * 60 * 6
    REDIS_JOB_EVENT_MAX_ITEMS: int = 1000
    REDIS_CONTEXT_TTL_SECONDS: int = 60 * 10
    REDIS_PATCH_SUCCESS_TTL_SECONDS: int = 60 * 20
    REDIS_PATCH_PROCESSING_TTL_SECONDS: int = 60 * 5

    # Context engine
    AGENT_CONTEXT_MAX_FILES: int = 3
    AGENT_CONTEXT_MAX_TOTAL_LINES: int = 260
    AGENT_CONTEXT_MAX_LINES_PER_FILE: int = 120
    AGENT_CONTEXT_MAX_INDEXED_FILES: int = 2000

    # OpenAI
    OPENAI_API_KEY: str | None = None
    OPENAI_BASE_URL: str = "https://api.siliconflow.com/v1"
    OPENAI_MODEL: str = "gpt-4o-mini"
    # Optional model overrides for tiered performance/quality.
    OPENAI_MODEL_CLASSIFIER: str | None = None
    OPENAI_MODEL_PLANNER: str | None = None
    OPENAI_MODEL_EXECUTOR: str | None = None
    OPENAI_MODEL_DEBUG: str | None = None
    OPENAI_MODEL_SUMMARY: str | None = None

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
    )

    @property
    def wait_timeout_seconds(self) -> int:
        """Clamped wait window.

        The brief requires the wait timeout be configurable between 30 and 60
        minutes.  Any ``WAIT_TIMEOUT_SECONDS`` value outside ``[1800, 3600]``
        is clamped to the nearest bound so a misconfigured environment can
        never produce an unsafe (instant or effectively-infinite) wait.
        """
        raw = self.WAIT_TIMEOUT_SECONDS
        try:
            value = int(raw)
        except (TypeError, ValueError):
            return 1800
        return max(1800, min(3600, value))


@lru_cache
def get_settings() -> Settings:
    return Settings()
