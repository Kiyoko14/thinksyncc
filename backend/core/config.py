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

    # Supabase
    SUPABASE_URL: str
    SUPABASE_ANON_KEY: str
    SUPABASE_SERVICE_ROLE_KEY: str
    
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


@lru_cache
def get_settings() -> Settings:
    return Settings()
