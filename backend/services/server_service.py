from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from fastapi import HTTPException, status
from postgrest.exceptions import APIError

from core.crypto import decrypt_secret, encrypt_secret
from core.database import get_supabase, get_supabase_async
from core.value_coercion import value_to_str
from models.server import ServerCreate, ServerResponse
from services.ssh_service import SSHService

logger = logging.getLogger(__name__)

_CAPABILITY_CACHE: dict[str, dict[str, Any]] = {}

class PlatformContextError(Exception):
    """Raised when required platform context fields are missing or unresolvable."""

    def __init__(self, message: str, missing: list[str]) -> None:
        super().__init__(message)
        self.missing: list[str] = list(missing)


@dataclass
class WorkspaceContext:
    """Authoritative platform context for one workspace execution."""

    workspace_id: str
    port: int | None = None
    subdomain: str | None = None
    protocol: str = "http"
    gateway_available: bool = False
    ssl_enabled: bool = False
    runtime_type: str | None = None

    @property
    def base_url(self) -> str | None:
        if not self.subdomain:
            return None
        return f"{self.protocol}://{self.subdomain}"

    @property
    def local_url(self) -> str | None:
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
        missing = self.missing_fields()
        if missing:
            raise PlatformContextError(
                f"Platform context incomplete for deployment — missing: {missing}.",
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

class ServerService:
    @staticmethod
    def _api_error_code(exc: APIError) -> str:
        code = getattr(exc, "code", None)
        if isinstance(code, str) and code:
            return code.upper()

        first_arg = exc.args[0] if exc.args else None
        if isinstance(first_arg, dict):
            raw_code = first_arg.get("code")
            if isinstance(raw_code, str):
                return raw_code.upper()

        return ""

    @staticmethod
    def _validate_uuid(value: str, field_name: str) -> None:
        try:
            UUID(value)
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid {field_name} format",
            )

    @staticmethod
    def list_servers(user_id: str) -> list[ServerResponse]:
        supabase = get_supabase()
        result = (
            supabase.table("servers")
            .select("*")
            .eq("user_id", user_id)
            .execute()
        )
        return [ServerResponse(**row) for row in result.data]

    @staticmethod
    def get_server(server_id: str, user_id: str) -> dict[str, Any]:
        ServerService._validate_uuid(server_id, "server_id")
        ServerService._validate_uuid(user_id, "user_id")
        supabase = get_supabase()
        try:
            result = (
                supabase.table("servers")
                .select("*")
                .eq("id", server_id)
                .eq("user_id", user_id)
                .maybe_single()
                .execute()
            )
        except APIError:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Server not found",
            )

        if not result or not result.data:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Server not found",
            )

        server = dict(result.data)
        server["ssh_key"] = decrypt_secret(server.get("ssh_key"))
        server["ssh_password"] = decrypt_secret(server.get("ssh_password"))
        return server

    @staticmethod
    async def create_server(user_id: str, data: ServerCreate) -> ServerResponse:
        ServerService._validate_uuid(user_id, "user_id")
        ssh_auth_method = value_to_str(getattr(data, "ssh_auth_method", None))
        await SSHService.validate_server_connection(
            host=data.host,
            port=data.ssh_port,
            username=data.ssh_user,
            auth_method=ssh_auth_method,
            ssh_password=data.ssh_password,
            ssh_key=data.ssh_key,
        )
        supabase = await get_supabase_async()
        record = {
            "user_id": user_id,
            "name": data.name,
            "host": data.host,
            "ssh_user": data.ssh_user,
            "ssh_port": data.ssh_port,
            "ssh_auth_method": ssh_auth_method,
            "ssh_key": encrypt_secret(data.ssh_key),
            "ssh_password": encrypt_secret(data.ssh_password),
            "created_at": datetime.now(timezone.utc).isoformat(),
        }

        try:
            result = await supabase.table("servers").insert(record).execute()
        except APIError as exc:
            code = ServerService._api_error_code(exc)
            if code in {"22P02"}:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Invalid request data",
                )
            if code in {"42501", "23503"}:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Not allowed to create server",
                )
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to create server",
            )

        if not result or not result.data:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to create server",
            )

        return ServerResponse(**result.data[0])

    @staticmethod
    def delete_server(server_id: str, user_id: str) -> None:
        ServerService._validate_uuid(server_id, "server_id")
        ServerService._validate_uuid(user_id, "user_id")
        supabase = get_supabase()
        try:
            result = (
                supabase.table("servers")
                .delete()
                .eq("id", server_id)
                .eq("user_id", user_id)
                .execute()
            )
        except APIError as exc:
            code = ServerService._api_error_code(exc)
            if code == "22P02":
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Invalid request data",
                )
            if code in {"42501", "23503"}:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Not allowed to delete server",
                )
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to delete server",
            )
        if not result.data:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Server not found",
            )

    @staticmethod
    async def detect_capabilities(server: dict[str, Any]) -> dict[str, Any]:
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

    @staticmethod
    def _sanitize_name(raw: str) -> str:
        cleaned = "".join(ch for ch in (raw or "").strip().lower() if ch.isalnum())
        return cleaned[:10] or "ws"

    @staticmethod
    async def load_workspace_context(
        *,
        workspace_id: str,
        workspace: dict[str, Any],
        server: dict[str, Any],
        capabilities: dict[str, Any] | None = None,
    ) -> WorkspaceContext:
        from services.redis_service import RedisService
        r = RedisService.get_sync_client()
        raw_port = r.get(f"ws:{workspace_id}:port")
        port: int | None = None
        if raw_port is not None:
            port = int(raw_port)
        gateway_available = bool(r.sismember("ws:active", workspace_id))
        logger.info(
            "[platform_context] Redis | workspace_id=%s | port=%s | gateway=%s",
            workspace_id, port, gateway_available,
        )

        domain = str(workspace.get("domain") or "").strip().lower()
        name = str(workspace.get("name") or "").strip().lower()
        slug = str(workspace.get("slug") or "").strip().lower()

        subdomain: str | None = None
        if domain:
            subdomain = domain
        elif name and slug:
            subdomain = f"{ServerService._sanitize_name(name)}-{slug}"
        elif slug:
            subdomain = slug

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

        cap = capabilities or {}
        runtime_type: str | None = None
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
