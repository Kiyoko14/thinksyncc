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

    # Sprint 4C/4D/4E: Decision Engine mode.
    #   Legacy bool (4C): DECISION_ENGINE_SHADOW — kept for backward compat.
    #   Tri/quad-state:   DECISION_ENGINE_MODE — "off"|"shadow"|"weighted"|"authoritative".
    #     off           -> current production behavior; engine not computed.
    #     shadow        -> engine computes + records MATCH/MISMATCH only (4C).
    #     weighted      -> engine computes a RECOMMENDATION + records agreement/
    #                      compatibility/safety classification (4D). Execution
    #                      still follows the existing orchestration.
    #     authoritative -> engine SELECTS the execution route; legacy validates
    #                      it; execution proceeds only after validation (4E).
    #                      Security gates remain absolute and independent; legacy
    #                      validation vetoes only UNSAFE escalation, always
    #                      explicitly classified — never a silent override.
    DECISION_ENGINE_SHADOW: bool = False
    DECISION_ENGINE_MODE: str = "authoritative"

    @property
    def decision_engine_mode(self) -> str:
        """Normalized mode, honouring the legacy bool.

        Precedence: an explicit valid DECISION_ENGINE_MODE wins. If it is left
        at the default "off" but the legacy DECISION_ENGINE_SHADOW bool is True,
        the mode resolves to "shadow" so 4C deployments keep working unchanged.
        Any unrecognized value clamps to "off" (fail safe).
        """
        raw = (self.DECISION_ENGINE_MODE or "").strip().lower()
        if raw in ("off", "shadow", "weighted", "authoritative"):
            if raw == "off" and self.DECISION_ENGINE_SHADOW:
                return "shadow"
            return raw
        return "shadow" if self.DECISION_ENGINE_SHADOW else "off"

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

    # GitHub App integration (backend-first; production-ready).
    # The App PEM is NEVER stored in the database — it lives here (env only)
    # and is used in-process to mint short-lived installation tokens.
    GITHUB_APP_ID: str = ""
    GITHUB_APP_SLUG: str = ""  # the App's slug, e.g. "thinksync-ai"
    GITHUB_APP_CLIENT_ID: str = ""
    # Client secret — env only, never in DB. Used only for the alternative
    # OAuth ``code`` exchange path; the primary installation flow needs none.
    GITHUB_APP_CLIENT_SECRET: str = ""
    # PEM (contents) OR a path to the PEM file. Exactly one should be set.
    GITHUB_APP_PRIVATE_KEY: str = ""
    GITHUB_APP_PRIVATE_KEY_PATH: str = ""
    GITHUB_APP_WEBHOOK_SECRET: str = ""
    # Where GitHub redirects the browser back to after the user authorizes.
    GITHUB_APP_REDIRECT_URI: str = "http://localhost:3000/github/callback"
    # Base URL for the GitHub REST/GraphQL API. Overridable for GHE.
    GITHUB_API_BASE: str = "https://api.github.com"
    GITHUB_OAUTH_BASE: str = "https://github.com"
    # Installation token TTL hard-cap (GitHub allows <= 1h). We refresh early.
    GITHUB_APP_TOKEN_TTL_SECONDS: int = 3300  # 55 minutes

    # --- GitHub API retry / backoff (Part 2) --------------------------------
    # Applied only to calls the caller marks retry=True (transient errors only:
    # network timeout / connection reset / HTTP 502/503/504). Rate limiting
    # (429 / Retry-After) is handled separately in Part 3.
    GITHUB_API_MAX_RETRIES: int = 3
    GITHUB_API_BACKOFF_BASE_SECONDS: float = 0.5
    GITHUB_API_BACKOFF_CAP_SECONDS: float = 8.0
    GITHUB_API_BACKOFF_JITTER_SECONDS: float = 0.3
    GITHUB_API_TIMEOUT_SECONDS: float = 30.0

    # --- GitHub API rate limiting (Part 3) ----------------------------------
    # Rate limiting is an INDEPENDENT layer ON TOP OF the Part 2 retry wrapper;
    # it does not modify retry/backoff/timeout behaviour.
    #
    # GITHUB_RATE_LIMIT_MAX_WAIT_SECONDS: the upper bound (in seconds) the
    #   backend is willing to wait for a rate-limit window to reset before
    #   giving up. When the computed wait (from Retry-After or X-RateLimit-Reset)
    #   is <= this cap, the request waits ONCE and retries. When it exceeds the
    #   cap, the backend does NOT wait and raises GitHubRateLimitError (-> HTTP
    #   429) immediately so the caller gets a fast, accurate error. Never a loop.
    #
    # GITHUB_RATE_LIMIT_RESPECT_RETRY_AFTER: when True, the Retry-After header
    #   takes precedence over X-RateLimit-Reset for computing the wait time.
    GITHUB_RATE_LIMIT_MAX_WAIT_SECONDS: float = 30.0
    GITHUB_RATE_LIMIT_RESPECT_RETRY_AFTER: bool = True

    @property
    def github_app_enabled(self) -> bool:
        return bool(self.GITHUB_APP_ID and (self.GITHUB_APP_PRIVATE_KEY or self.GITHUB_APP_PRIVATE_KEY_PATH))

    # OpenAI / LLM provider
    OPENAI_API_KEY: str | None = None
    OPENAI_BASE_URL: str = "https://api.novita.ai/openai/v1"
    # Base model. No hardcoded default — must be configured via OPENAI_MODEL
    # (or a role-specific override). resolve_model() raises if nothing is set.
    OPENAI_MODEL: str | None = None
    # Optional role-specific model overrides for tiered performance/quality.
    # Each is optional; when unset, resolve_model() falls back to OPENAI_MODEL.
    OPENAI_MODEL_CLASSIFIER: str | None = None
    OPENAI_MODEL_PLANNER: str | None = None
    OPENAI_MODEL_EXECUTOR: str | None = None
    OPENAI_MODEL_DEBUG: str | None = None
    OPENAI_MODEL_SUMMARY: str | None = None
    OPENAI_MODEL_CODE: str | None = None
    OPENAI_MODEL_REASONING: str | None = None
    OPENAI_MODEL_VISION: str | None = None
    OPENAI_MODEL_EMBEDDING: str | None = None

    # Maps a logical role to its optional dedicated config field. Roles not
    # listed (e.g. "chat", "patch", "revision") fall back to OPENAI_MODEL.
    _MODEL_ROLE_FIELDS: dict[str, str] = {
        "classifier": "OPENAI_MODEL_CLASSIFIER",
        "planner": "OPENAI_MODEL_PLANNER",
        "executor": "OPENAI_MODEL_EXECUTOR",
        "debug": "OPENAI_MODEL_DEBUG",
        "summary": "OPENAI_MODEL_SUMMARY",
        "code": "OPENAI_MODEL_CODE",
        "reasoning": "OPENAI_MODEL_REASONING",
        "vision": "OPENAI_MODEL_VISION",
        "embedding": "OPENAI_MODEL_EMBEDDING",
    }

    def resolve_model(self, role: str) -> str:
        """Resolve the model name for a logical role from configuration only.

        Fallback order:
            1. role-specific override (e.g. OPENAI_MODEL_PLANNER)
            2. base OPENAI_MODEL
            3. ValueError  — nothing configured

        The planner (and every other caller) MUST obtain its model through this
        method. No hardcoded model names may appear in application code.
        """
        field = self._MODEL_ROLE_FIELDS.get(role)
        if field:
            specific = getattr(self, field, None)
            if specific:
                return specific
        if self.OPENAI_MODEL:
            return self.OPENAI_MODEL
        raise ValueError(
            f"No model configured for role '{role}' and OPENAI_MODEL is not set. "
            "Set OPENAI_MODEL or a role-specific OPENAI_MODEL_<ROLE> override."
        )

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
