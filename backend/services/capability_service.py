"""Capability resolver and platform context loader for ThinkSync workspaces.

Public API:
  detect_capabilities(server)       — runtime tool detection (python/node/npm/pm2/…)
  load_workspace_context(...)       — authoritative platform context (port/subdomain/SSL/gateway)
  WorkspaceContext                  — dataclass carrying resolved platform state
  PlatformContextError              — raised when required fields are missing
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from services.ssh_service import SSHService
logger = logging.getLogger(__name__)

_CAPABILITY_CACHE: dict[str, dict[str, Any]] = {}


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class PlatformContextError(Exception):
    """Raised when required platform context fields are missing or unresolvable.

    Attributes:
        missing: list of field names that could not be resolved.
    """

    def __init__(self, message: str, missing: list[str]) -> None:
        super().__init__(message)
        self.missing: list[str] = list(missing)


# ---------------------------------------------------------------------------
# WorkspaceContext — single source of truth for platform state
# ---------------------------------------------------------------------------

@dataclass
class WorkspaceContext:
    """Authoritative platform context for one workspace execution.

    Every field comes from a real source:
      port             — Redis key ``ws:{workspace_id}:port``
      runtime_type     — set from the workspace file detector before deployment
      protocol         — "https" if Let's-Encrypt cert present, else "http"
      gateway_available— workspace_id is member of Redis ``ws:active``
      ssl_enabled      — ``/etc/letsencrypt/live/{subdomain}/cert.pem`` exists
      runtime_type     — derived from capabilities (python > node > None)

    Never guessed.  Never hardcoded.  Missing fields reported via
    ``missing_fields()``; ``verify_for_deployment()`` raises if any are absent.
    """

    workspace_id: str
    port: int | None = None
    subdomain: str | None = None
    protocol: str = "http"
    gateway_available: bool = False
    ssl_enabled: bool = False
    runtime_type: str | None = None

    @property
    def base_url(self) -> str | None:
        """Authoritative public URL.  None when subdomain is unresolved."""
        if not self.subdomain:
            return None
        return f"{self.protocol}://{self.subdomain}"

    @property
    def local_url(self) -> str | None:
        """Local curl target for deployment verification.  None when port unknown."""
        if self.port is None:
            return None
        return f"http://127.0.0.1:{self.port}"

    def missing_fields(self) -> list[str]:
        missing: list[str] = []
        if not self.workspace_id:
            missing.append("workspace_id")
        if self.port is None:
            missing.append("port")
        if not self.subdomain:
            missing.append("subdomain")
        return missing

    def verify_for_deployment(self) -> None:
        """Raise :class:`PlatformContextError` if any required deployment field is absent."""
        missing = self.missing_fields()
        if missing:
            raise PlatformContextError(
                f"Platform context incomplete for deployment — missing: {missing}. "
                "Agent must STOP. Never guess, never fallback, never hallucinate.",
                missing=missing,
            )

    def as_dict(self) -> dict[str, Any]:
        return {
            "workspace_id": self.workspace_id,
            "port": self.port,
            "subdomain": self.subdomain,
            "protocol": self.protocol,
            "gateway_available": self.gateway_available,
            "ssl_enabled": self.ssl_enabled,
            "runtime_type": self.runtime_type,
            "base_url": self.base_url,
            "local_url": self.local_url,
        }


# ---------------------------------------------------------------------------
# Runtime capability detection
# ---------------------------------------------------------------------------

async def detect_capabilities(server: dict[str, Any]) -> dict[str, Any]:
    """Detect remote-server runtime capabilities (python, node, npm, pm2, …).

    Result is cached per server key for the lifetime of the process.
    """
    cache_key = f"{server.get('host')}|{server.get('ssh_user')}|{server.get('ssh_port')}"
    cached = _CAPABILITY_CACHE.get(cache_key)
    if isinstance(cached, dict) and cached:
        return dict(cached)

    commands: dict[str, str] = {
        "python": "python3 --version",
        "node": "node --version",
        "npm": "npm --version",
        "pm2": "pm2 -v",
        "docker": "docker --version",
        "git": "git --version",
        "nginx": "nginx -v",
    }

    capabilities: dict[str, Any] = {}
    versions: dict[str, str] = {}

    for key, cmd in commands.items():
        try:
            res = await SSHService.execute(server=server, command=cmd)
            capabilities[key] = res.exit_code == 0
            versions[key] = (res.stdout or res.stderr or "").strip()
        except Exception:
            capabilities[key] = False
            versions[key] = ""
    capabilities["versions"] = versions

    system_info_cmds: dict[str, str] = {
        "which_python3": "which python3",
        "which_node": "which node",
        "free_m": "free -m",
        "df_h": "df -h",
    }
    system_info: dict[str, str] = {}
    for key, cmd in system_info_cmds.items():
        try:
            res = await SSHService.execute(server=server, command=cmd)
            system_info[key] = (res.stdout or res.stderr or "").strip()
        except Exception:
            system_info[key] = ""
    capabilities["system_info"] = system_info

    _CAPABILITY_CACHE[cache_key] = dict(capabilities)
    return capabilities


# ---------------------------------------------------------------------------
# Platform context loader
# ---------------------------------------------------------------------------

def _sanitize_name(raw: str) -> str:
    """Coerce workspace name to strict alphanumeric, max 10 chars."""
    cleaned = "".join(ch for ch in (raw or "").strip().lower() if ch.isalnum())
    return cleaned[:10] or "ws"


async def load_workspace_context(
    *,
    workspace_id: str,
    workspace: dict[str, Any],
    server: dict[str, Any],
    capabilities: dict[str, Any] | None = None,
    runtime_type: str | None = None,
) -> WorkspaceContext:
    """Load authoritative platform context from Redis + workspace record + SSH.
      runtime_type     — set from the workspace file detector before deployment
    Sources (strict precedence):
      port             — Redis ``ws:{workspace_id}:port``
      gateway_available— Redis ``ws:active`` SISMEMBER
      subdomain        — ``workspace.domain``  →  ``name-slug``  →  ``slug``
      ssl_enabled      — SSH: ``test -f /etc/letsencrypt/live/{subdomain}/cert.pem``
      protocol         — "https" if ssl_enabled, else "http"
      runtime_type     — capabilities: python > node > None

    Missing fields are recorded; call ``verify_for_deployment()`` to surface
    them as a hard error before any deployment attempt.
    """
    # --- port + gateway availability from Redis ----------------------------
    port: int | None = None
    gateway_available = False
    try:
        from services.redis_service import RedisService
        r = RedisService.get_sync_client()
        raw_port = r.get(f"ws:{workspace_id}:port")
        if raw_port is not None:
            port = int(raw_port)
        gateway_available = bool(r.sismember("ws:active", workspace_id))
        logger.info(
            "[platform_context] Redis | workspace_id=%s | port=%s | gateway=%s",
            workspace_id, port, gateway_available,
        )
    except Exception as exc:
        logger.warning("[platform_context] Redis unavailable: %s", exc)

    # --- subdomain from DB record ------------------------------------------
    domain = str(workspace.get("domain") or "").strip().lower()
    name = str(workspace.get("name") or "").strip().lower()
    slug = str(workspace.get("slug") or "").strip().lower()

    subdomain: str | None = None
    if domain:
        subdomain = domain
    elif name and slug:
        subdomain = f"{_sanitize_name(name)}-{slug}"
    elif slug:
        subdomain = slug

    # --- SSL via SSH cert check --------------------------------------------
    ssl_enabled = False
    if subdomain:
        try:
            cert_check = await SSHService.execute(
                server=server,
                command=f"test -f /etc/letsencrypt/live/{subdomain}/cert.pem",
                command_timeout=5,
            )
            ssl_enabled = cert_check.exit_code == 0
        except Exception:
            ssl_enabled = False

    protocol = "https" if ssl_enabled else "http"

    # Deployment supplies the canonical project classification. The capability
    # fallback is retained only for legacy context consumers that do not yet
    # provide a file snapshot; it is never used by static deployment routing.
    if runtime_type is None:
        cap = capabilities or {}
        if cap.get("python"):
            runtime_type = "python"
        elif cap.get("node"):
            runtime_type = "node"

    ctx = WorkspaceContext(
        workspace_id=workspace_id,
        port=port,
        subdomain=subdomain,
        protocol=protocol,
        gateway_available=gateway_available,
        ssl_enabled=ssl_enabled,
        runtime_type=runtime_type,
    )
    logger.info("[platform_context] resolved | %s", ctx.as_dict())
    return ctx
