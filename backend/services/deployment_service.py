from datetime import datetime, timezone
import logging
import shlex
from typing import Any
from uuid import UUID

from fastapi import HTTPException, status
from postgrest.exceptions import APIError

from core.database import get_supabase
from services.port_allocator import allocate_port, release_port
from services.redis_service import RedisService
from services.server_service import ServerService
from services.ssh_service import SSHService

logger = logging.getLogger(__name__)


class DeploymentService:
    """Manage workspace deployments and domain→port mappings."""

    @staticmethod
    def _process_name(workspace_id: str) -> str:
        return f"ws-{workspace_id}"

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
    def _safe_workspace_path(path: str) -> str:
        cleaned = path.strip()
        if not cleaned:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Workspace path is missing",
            )
        if ".." in cleaned or "\n" in cleaned or "\r" in cleaned or not cleaned.startswith("/"):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid workspace path",
            )
        return cleaned

    @staticmethod
    def _deployment_command(workspace_path: str, port: int) -> str:
        quoted_path = shlex.quote(workspace_path)
        return f"cd {quoted_path} && python3 -m http.server {port} --bind 127.0.0.1"

    @staticmethod
    async def _ensure_pm2_installed(server: dict[str, Any]) -> None:
        # Install PM2 only when missing; keep this idempotent and distro-aware.
        ensure_command = (
            "command -v pm2 >/dev/null 2>&1 || "
            "("
            "if command -v npm >/dev/null 2>&1; then "
            "npm install -g pm2; "
            "elif command -v apt-get >/dev/null 2>&1; then "
            "export DEBIAN_FRONTEND=noninteractive; "
            "apt-get update -y && apt-get install -y nodejs npm && npm install -g pm2; "
            "elif command -v dnf >/dev/null 2>&1; then "
            "dnf install -y nodejs npm && npm install -g pm2; "
            "elif command -v yum >/dev/null 2>&1; then "
            "yum install -y nodejs npm && npm install -g pm2; "
            "else "
            "echo 'No supported package manager found to install pm2' >&2; "
            "exit 127; "
            "fi"
            ") && command -v pm2 >/dev/null 2>&1"
        )

        result = await DeploymentService._execute_remote(
            server=server,
            command=ensure_command,
            step="ensure_pm2_installed",
        )
        if result.exit_code != 0:
            raise DeploymentService._structured_command_error(
                step="ensure_pm2_installed",
                output=result.output,
                exit_code=result.exit_code,
            )

    @staticmethod
    def _structured_command_error(step: str, output: str, exit_code: int) -> HTTPException:
        return HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={
                "message": "Remote command failed",
                "step": step,
                "exit_code": exit_code,
                "output": (output or "")[:2000],
            },
        )

    @staticmethod
    async def _execute_remote(server: dict[str, Any], command: str, step: str) -> Any:
        try:
            return await SSHService.execute(server=server, command=command)
        except HTTPException as exc:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail={
                    "message": "SSH command execution failed",
                    "step": step,
                    "error": str(exc.detail),
                },
            )

    @staticmethod
    def _get_existing_deployment(supabase, workspace_id: str) -> dict[str, Any] | None:
        try:
            existing = (
                supabase.table("workspace_deployments")
                .select("*")
                .eq("workspace_id", workspace_id)
                .order("created_at", desc=True)
                .limit(1)
                .execute()
            )
        except APIError:
            return None

        if not existing or not existing.data:
            return None
        return existing.data[0]

    @staticmethod
    async def create_deployment(workspace_id: str, user_id: str) -> dict[str, Any]:
        """Create a deployment for a workspace."""
        DeploymentService._validate_uuid(workspace_id, "workspace_id")
        DeploymentService._validate_uuid(user_id, "user_id")

        from services.workspace_service import WorkspaceService

        workspace = WorkspaceService.get_workspace_by_id(id=workspace_id, user_id=user_id)
        server = ServerService.get_server(server_id=workspace["server_id"], user_id=user_id)
        workspace_path = DeploymentService._safe_workspace_path(str(workspace.get("path", "")))
        process_name = f"ws-{workspace_id}"

        await DeploymentService._ensure_pm2_installed(server=server)

        supabase = get_supabase()
        existing = DeploymentService._get_existing_deployment(supabase=supabase, workspace_id=workspace_id)

        port = allocate_port(workspace_id)

        run_command = DeploymentService._deployment_command(workspace_path=workspace_path, port=port)
        start_cmd = f'pm2 start "{run_command}" --name {process_name} --force'
        check_cmd = f"pm2 describe {process_name}"
        verify_cmd = f"curl -f http://127.0.0.1:{port}"

        try:
            existing_process = await DeploymentService._execute_remote(
                server=server,
                command=check_cmd,
                step="pm2_describe_existing",
            )
            process_exists = existing_process.exit_code == 0

            pm2_result = await DeploymentService._execute_remote(
                server=server,
                command=start_cmd,
                step="pm2_start",
            )
            if pm2_result.exit_code != 0:
                raise DeploymentService._structured_command_error(
                    step="pm2_start",
                    output=pm2_result.output,
                    exit_code=pm2_result.exit_code,
                )

            check_result = await DeploymentService._execute_remote(
                server=server,
                command=check_cmd,
                step="pm2_describe",
            )
            if check_result.exit_code != 0 or "online" not in (check_result.output or "").lower():
                raise HTTPException(500, "PM2 process not running")

            verify_result = await DeploymentService._execute_remote(
                server=server,
                command=verify_cmd,
                step="verify_http_server",
            )
            if verify_result.exit_code != 0 and process_exists:
                restart_result = await DeploymentService._execute_remote(
                    server=server,
                    command=f"pm2 restart {process_name}",
                    step="pm2_restart",
                )
                if restart_result.exit_code != 0:
                    raise DeploymentService._structured_command_error(
                        step="pm2_restart",
                        output=restart_result.output,
                        exit_code=restart_result.exit_code,
                    )

                check_result = await DeploymentService._execute_remote(
                    server=server,
                    command=check_cmd,
                    step="pm2_describe_after_restart",
                )
                if check_result.exit_code != 0 or "online" not in (check_result.output or "").lower():
                    raise HTTPException(500, "PM2 process not running")

                verify_result = await DeploymentService._execute_remote(
                    server=server,
                    command=verify_cmd,
                    step="verify_http_server_after_restart",
                )

            if verify_result.exit_code != 0:
                raise HTTPException(502, "Workspace failed to start")

            check = await SSHService.execute(
                server=server,
                command=f"curl -f http://127.0.0.1:{port}",
            )
            if check.exit_code != 0:
                raise HTTPException(502, "Port not serving traffic")
        except HTTPException:
            release_port(workspace_id)
            raise
        except Exception:
            release_port(workspace_id)
            raise

        # Sync deployment state into Redis (single source of truth for the gateway).
        # Order is critical: PM2 start → verify_http_server OK → Redis write.
        from services.workspace_service import WorkspaceService

        normalized_name = WorkspaceService._sanitize_workspace_name(str(workspace.get("name") or ""))
        slug = str(workspace.get("slug") or "").strip().lower()
        subdomain = f"{normalized_name}-{slug}"

        from services.redis_service import RedisService

        r = RedisService.get_sync_client()

        pipe = r.pipeline()
        pipe.set(f"ws:{workspace_id}:port", port)
        pipe.set(f"ws_domain:{subdomain}", workspace_id)
        pipe.sadd("ws:active", workspace_id)

        try:
            pipe.execute()
        except Exception:
            release_port(workspace_id)
            raise HTTPException(
                status_code=500,
                detail="Critical: Redis sync failed",
            )

        logger.info(
            "DEPLOY OK | ws=%s port=%s process=%s",
            workspace_id, port, process_name
        )

        if existing:
            payload = {
                "port": port,
                "is_active": True,
            }
            query = supabase.table("workspace_deployments").update(payload).eq("id", existing["id"])
        else:
            payload = {
                "workspace_id": workspace_id,
                "port": port,
                "is_active": True,
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
            query = supabase.table("workspace_deployments").insert(payload)

        try:
            result = query.execute()
        except APIError as exc:
            code = DeploymentService._api_error_code(exc)
            if code in {"23503", "42501"}:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Access denied or workspace not found",
                )
            if code == "22P02":
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Invalid request data",
                )
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to create deployment",
            )

        if not result:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to persist deployment state",
            )

        return {
            "workspace_id": workspace_id,
            "port": port,
            "domain": f"{subdomain}.{WorkspaceService._base_domain()}",
            "slug": workspace.get("slug"),
            "is_active": True,
        }

    @staticmethod
    def get_deployment(workspace_id: str, user_id: str) -> dict[str, Any] | None:
        """Get active deployment for a workspace."""
        DeploymentService._validate_uuid(workspace_id, "workspace_id")
        DeploymentService._validate_uuid(user_id, "user_id")

        from services.workspace_service import WorkspaceService

        workspace = WorkspaceService.get_workspace_by_id(id=workspace_id, user_id=user_id)

        supabase = get_supabase()
        result = (
            supabase.table("workspace_deployments")
            .select("*")
            .eq("workspace_id", workspace_id)
            .eq("is_active", True)
            .limit(1)
            .execute()
        )

        if not result or not result.data:
            return None

        deployment = result.data[0]
        normalized_name = WorkspaceService._sanitize_workspace_name(str(workspace.get("name") or ""))
        slug = str(workspace.get("slug") or "").strip().lower()
        subdomain = f"{normalized_name}-{slug}"
        return {
            "workspace_id": workspace_id,
            "port": deployment["port"],
            "domain": f"{subdomain}.{WorkspaceService._base_domain()}",
            "slug": workspace.get("slug"),
            "is_active": deployment["is_active"],
        }

    @staticmethod
    async def deactivate_deployment(workspace_id: str, user_id: str) -> None:
        """Deactivate workspace deployment."""
        DeploymentService._validate_uuid(workspace_id, "workspace_id")
        DeploymentService._validate_uuid(user_id, "user_id")

        from services.workspace_service import WorkspaceService

        workspace = WorkspaceService.get_workspace_by_id(id=workspace_id, user_id=user_id)
        server = ServerService.get_server(server_id=workspace["server_id"], user_id=user_id)

        process_name = DeploymentService._process_name(workspace_id)
        stop_result = await DeploymentService._execute_remote(
            server=server,
            command=f"pm2 delete {shlex.quote(process_name)}",
            step="pm2_delete",
        )
        stop_output = (stop_result.output or "").lower()
        if stop_result.exit_code != 0 and "process or namespace" not in stop_output:
            raise DeploymentService._structured_command_error(
                step="pm2_delete",
                output=stop_result.output,
                exit_code=stop_result.exit_code,
            )

        supabase = get_supabase()
        try:
            supabase.table("workspace_deployments").update({"is_active": False}).eq(
                "workspace_id", workspace_id
            ).execute()
        except APIError:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to deactivate deployment",
            )
