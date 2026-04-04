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
    CORS_ORIGINS: list[str] = ["http://104.248.90.38:3000"]

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

    # OpenAI
    OPENAI_API_KEY: str | None = None
    OPENAI_MODEL: str = "gpt-4o-mini"

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()
