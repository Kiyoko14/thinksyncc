import asyncio
import inspect
from datetime import datetime, timezone
import logging
from contextlib import asynccontextmanager
from collections.abc import Awaitable, Callable
from typing import Any

import asyncssh
from fastapi import HTTPException, status

from core.config import get_settings
from models.message import CommandResponse

logger = logging.getLogger(__name__)


class SSHService:
    # NOTE: Keep SSH connect config minimal and compatible.
    # Do not set custom kex/encryption/mac algorithm lists unless you have a
    # proven, version-pinned need.

    @staticmethod
    async def _emit_output_chunk(
        callback: Callable[[str, str], Awaitable[None] | None] | None,
        stream: str,
        chunk: str,
    ) -> None:
        if callback is None or not chunk:
            return
        maybe_awaitable = callback(stream, chunk)
        if inspect.isawaitable(maybe_awaitable):
            await maybe_awaitable

    @staticmethod
    def _http_error(
        *,
        status_code: int,
        code: str,
        message: str,
        meta: dict[str, Any] | None = None,
    ) -> HTTPException:
        detail: dict[str, Any] = {"code": code, "message": message}
        if meta:
            detail["meta"] = meta
        return HTTPException(status_code=status_code, detail=detail)

    @staticmethod
    def _require_nonempty_str(value: Any, field: str) -> str:
        if not isinstance(value, str):
            raise SSHService._http_error(
                status_code=status.HTTP_400_BAD_REQUEST,
                code="COMMAND_FAILED",
                message=f"{field} must be a string",
            )
        cleaned = value.strip()
        if not cleaned:
            raise SSHService._http_error(
                status_code=status.HTTP_400_BAD_REQUEST,
                code="COMMAND_FAILED",
                message=f"{field} is required",
            )
        return cleaned

    @staticmethod
    def _require_port(value: Any, field: str = "port") -> int:
        try:
            port = int(value)
        except (TypeError, ValueError):
            raise SSHService._http_error(
                status_code=status.HTTP_400_BAD_REQUEST,
                code="HOST_UNREACHABLE",
                message=f"{field} must be an integer",
            )
        if port < 1 or port > 65535:
            raise SSHService._http_error(
                status_code=status.HTTP_400_BAD_REQUEST,
                code="HOST_UNREACHABLE",
                message=f"{field} must be between 1 and 65535",
            )
        return port

    @staticmethod
    def _validate_auth(
        *,
        auth_method: str,
        ssh_password: str | None,
        ssh_key: str | None,
        context: str,
    ) -> dict[str, Any]:
        method = SSHService._require_nonempty_str(auth_method, f"{context}.auth_method")
        if method not in ("password", "key"):
            raise SSHService._http_error(
                status_code=status.HTTP_400_BAD_REQUEST,
                code="AUTH_FAILED",
                message=f"Unsupported auth method: {method}",
            )

        if method == "password":
            if ssh_key and ssh_key.strip():
                raise SSHService._http_error(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    code="AUTH_FAILED",
                    message="Provide either password or key, not both",
                )
            password = (ssh_password or "").strip()
            if not password:
                raise SSHService._http_error(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    code="AUTH_FAILED",
                    message="SSH password is required for password authentication",
                )
            return {"password": password}

        # key auth
        if ssh_password and ssh_password.strip():
            raise SSHService._http_error(
                status_code=status.HTTP_400_BAD_REQUEST,
                code="AUTH_FAILED",
                message="Provide either password or key, not both",
            )
        key_text = (ssh_key or "").strip()
        if not key_text:
            raise SSHService._http_error(
                status_code=status.HTTP_400_BAD_REQUEST,
                code="AUTH_FAILED",
                message="SSH key is required for key authentication",
            )
        try:
            imported = asyncssh.import_private_key(key_text)
        except asyncssh.KeyImportError:
            raise SSHService._http_error(
                status_code=status.HTTP_400_BAD_REQUEST,
                code="AUTH_FAILED",
                message="Invalid SSH private key",
            )
        return {"client_keys": [imported]}

    @staticmethod
    def _sanitize_connect_kwargs(connect_kwargs: dict[str, Any]) -> dict[str, Any]:
        sanitized: dict[str, Any] = dict(connect_kwargs)
        if "password" in sanitized:
            sanitized["password"] = "<redacted>"
        if "client_keys" in sanitized:
            try:
                sanitized["client_keys"] = f"<redacted:{len(sanitized['client_keys'])}>"
            except Exception:  # noqa: BLE001
                sanitized["client_keys"] = "<redacted>"
        return sanitized

    @staticmethod
    def _classify_connect_error(exc: BaseException) -> tuple[str, int, str]:
        # Required categories: AUTH_FAILED, NETWORK_ERROR, SSH_ERROR, TIMEOUT
        if isinstance(exc, asyncssh.PermissionDenied):
            return ("AUTH_FAILED", status.HTTP_400_BAD_REQUEST, str(exc) or "SSH authentication failed")
        if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
            return ("TIMEOUT", status.HTTP_504_GATEWAY_TIMEOUT, str(exc) or "SSH connection timed out")
        if isinstance(exc, OSError):
            return ("NETWORK_ERROR", status.HTTP_502_BAD_GATEWAY, str(exc) or "Network error")
        if isinstance(exc, asyncssh.Error):
            return ("SSH_ERROR", status.HTTP_502_BAD_GATEWAY, str(exc) or "SSH error")
        return ("SSH_ERROR", status.HTTP_502_BAD_GATEWAY, str(exc) or "SSH error")

    @staticmethod
    def _classify_command_error(exc: BaseException) -> tuple[str, int, str]:
        if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
            return ("TIMEOUT", status.HTTP_504_GATEWAY_TIMEOUT, str(exc) or "SSH command timed out")
        if isinstance(exc, asyncssh.PermissionDenied):
            return ("AUTH_FAILED", status.HTTP_400_BAD_REQUEST, str(exc) or "SSH authentication failed")
        if isinstance(exc, OSError):
            return ("NETWORK_ERROR", status.HTTP_502_BAD_GATEWAY, str(exc) or "Network error")
        if isinstance(exc, asyncssh.Error):
            return ("SSH_ERROR", status.HTTP_502_BAD_GATEWAY, str(exc) or "SSH command failed")
        return ("SSH_ERROR", status.HTTP_502_BAD_GATEWAY, str(exc) or "SSH command failed")

    @staticmethod
    @asynccontextmanager
    async def _connect(
        *,
        server_id: Any,
        connect_kwargs: dict[str, Any],
        operation: str,
    ):
        settings = get_settings()
        sanitized = SSHService._sanitize_connect_kwargs(connect_kwargs)
        logger.info(
            "[ssh] connect | op=%s | server_id=%s | connect_kwargs=%s",
            operation,
            server_id,
            sanitized,
        )
        try:
            conn = await asyncio.wait_for(asyncssh.connect(**connect_kwargs), timeout=settings.SSH_TIMEOUT)
        except BaseException as exc:  # noqa: BLE001
            logger.error(
                "[ssh] connect_failed | op=%s | server_id=%s | err_type=%s | err=%r | connect_kwargs=%s",
                operation,
                server_id,
                type(exc).__name__,
                str(exc),
                sanitized,
                exc_info=True,
            )
            raise

        try:
            logger.info("[ssh] connect_ok | op=%s | server_id=%s", operation, server_id)
            yield conn
        finally:
            try:
                conn.close()
                await conn.wait_closed()
            except Exception:  # noqa: BLE001
                logger.debug(
                    "[ssh] conn_close_failed | op=%s | server_id=%s",
                    operation,
                    server_id,
                    exc_info=True,
                )

    @staticmethod
    async def validate_server_connection(
        host: str,
        port: int,
        username: str,
        auth_method: str,
        ssh_password: str | None = None,
        ssh_key: str | None = None,
    ) -> None:
        settings = get_settings()

        host_clean = SSHService._require_nonempty_str(host, "host")
        port_int = SSHService._require_port(port, "port")
        username_clean = SSHService._require_nonempty_str(username, "username")
        auth_kwargs = SSHService._validate_auth(
            auth_method=auth_method,
            ssh_password=ssh_password,
            ssh_key=ssh_key,
            context="ssh",
        )

        # Minimal working config (no custom algorithms; no host key checks).
        connect_kwargs: dict[str, Any] = {
            "host": host_clean,
            "port": port_int,
            "username": username_clean,
            "known_hosts": None,
            **auth_kwargs,
        }

        try:
            logger.info(
                "[ssh] validate_server_connection | host=%s | port=%s | username=%s | auth_method=%s",
                host_clean,
                port_int,
                username_clean,
                auth_method,
            )
            async with SSHService._connect(
                server_id="validate",
                connect_kwargs=connect_kwargs,
                operation="validate_server_connection",
            ) as conn:
                result = await conn.run(
                    "echo thinksync-ssh-check",
                    timeout=settings.SSH_COMMAND_TIMEOUT,
                    check=False,
                )
                if result.exit_status not in (0, None):
                    raise SSHService._http_error(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        code="COMMAND_FAILED",
                        message="SSH server is reachable but command execution failed",
                    )
        except HTTPException:
            raise
        except BaseException as exc:  # noqa: BLE001
            code, http_status, message = SSHService._classify_connect_error(exc)
            raise SSHService._http_error(
                status_code=http_status,
                code=code,
                message=message,
                meta={"error_type": type(exc).__name__},
            )

    @staticmethod
    async def execute(
        server: dict[str, Any],
        command: str,
        command_timeout: int | None = None,
        on_output_chunk: Callable[[str, str], Awaitable[None] | None] | None = None,
    ) -> CommandResponse:
        settings = get_settings()
        effective_timeout = command_timeout or settings.SSH_COMMAND_TIMEOUT

        server_id = server.get("id")
        host_clean = SSHService._require_nonempty_str(server.get("host"), "server.host")
        port_int = SSHService._require_port(server.get("ssh_port"), "server.ssh_port")
        username_clean = SSHService._require_nonempty_str(server.get("ssh_user"), "server.ssh_user")
        command_clean = SSHService._require_nonempty_str(command, "command")
        auth_method = SSHService._require_nonempty_str(
            server.get("ssh_auth_method"), "server.ssh_auth_method"
        )

        if auth_method == "password":
            auth_kwargs = SSHService._validate_auth(
                auth_method="password",
                ssh_password=server.get("ssh_password"),
                ssh_key=None,
                context="server",
            )
        elif auth_method == "key":
            auth_kwargs = SSHService._validate_auth(
                auth_method="key",
                ssh_password=None,
                ssh_key=server.get("ssh_key"),
                context="server",
            )
        else:
            raise SSHService._http_error(
                status_code=status.HTTP_400_BAD_REQUEST,
                code="AUTH_FAILED",
                message=f"Unsupported auth method: {auth_method}",
            )

        # Minimal working config (no custom algorithms; no host key checks).
        connect_kwargs: dict[str, Any] = {
            "host": host_clean,
            "port": port_int,
            "username": username_clean,
            "known_hosts": None,
            **auth_kwargs,
        }

        try:
            logger.info(
                "[ssh] execute | server_id=%s | host=%s | port=%s | user=%s | connect_timeout=%s | command_timeout=%s | command=%r",
                server_id,
                host_clean,
                port_int,
                username_clean,
                settings.SSH_TIMEOUT,
                effective_timeout,
                command_clean,
            )
            async with SSHService._connect(
                server_id=server_id,
                connect_kwargs=connect_kwargs,
                operation="execute",
            ) as conn:
                result = await conn.run(command_clean, check=False, timeout=effective_timeout)
                stdout = result.stdout or ""
                stderr = result.stderr or ""
                exit_code = int(result.exit_status or 0)
                logger.info(
                    "[ssh] result | server_id=%s | exit_code=%s | stdout=%r | stderr=%r",
                    server_id,
                    exit_code,
                    stdout[:800],
                    stderr[:400],
                )
                return CommandResponse(
                    server_id=str(server["id"]),
                    command=command_clean,
                    stdout=stdout,
                    stderr=stderr,
                    output=stdout or stderr or "",
                    exit_code=exit_code,
                    executed_at=datetime.now(timezone.utc),
                )
        except HTTPException:
            raise
        except BaseException as exc:  # noqa: BLE001
            code, http_status, message = SSHService._classify_command_error(exc)
            raise SSHService._http_error(
                status_code=http_status,
                code=code,
                message=message,
                meta={"error_type": type(exc).__name__},
            )
