from datetime import datetime, timezone
import shlex
from typing import Any
from uuid import UUID

from fastapi import HTTPException, status
from postgrest.exceptions import APIError

from core.database import get_supabase
from services.server_service import ServerService
from services.ssh_service import SSHService


class DeploymentService:
    """Manage workspace deployments and domain→port mappings."""

    _MIN_PORT = 10000
    _MAX_PORT = 65535

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
    def get_next_available_port() -> int:
        """Allocate next available port from pool."""
        supabase = get_supabase()
        try:
            result = (
                supabase.table("workspace_deployments")
                .select("port")
                .eq("is_active", True)
                .execute()
            )

            used_ports = {
                row.get("port")
                for row in (result.data or [])
                if isinstance(row.get("port"), int)
            }
            for port in range(DeploymentService._MIN_PORT, DeploymentService._MAX_PORT + 1):
                if port not in used_ports:
                    return port
        except APIError as exc:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail={
                    "message": "Failed to allocate deployment port",
                    "error": str(exc),
                },
            )

        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="No ports available for deployment",
        )

    @staticmethod
    async def create_deployment(workspace_id: str, user_id: str) -> dict[str, Any]:
        """Create a deployment for a workspace."""
        DeploymentService._validate_uuid(workspace_id, "workspace_id")
        DeploymentService._validate_uuid(user_id, "user_id")

        from services.workspace_service import WorkspaceService

        workspace = WorkspaceService.get_workspace_by_id(id=workspace_id, user_id=user_id)
        server = ServerService.get_server(server_id=workspace["server_id"], user_id=user_id)
        workspace_path = DeploymentService._safe_workspace_path(str(workspace.get("path", "")))
        process_name = DeploymentService._process_name(workspace_id)

        await DeploymentService._ensure_pm2_installed(server=server)

        supabase = get_supabase()
        existing = DeploymentService._get_existing_deployment(supabase=supabase, workspace_id=workspace_id)

        if existing and isinstance(existing.get("port"), int):
            port = existing["port"]
        else:
            port = DeploymentService.get_next_available_port()

        run_command = DeploymentService._deployment_command(workspace_path=workspace_path, port=port)

        # If the PM2 process already exists, restart it; otherwise create it.
        pm2_command = (
            f'pm2 describe {shlex.quote(process_name)} >/dev/null 2>&1 && '
            f'pm2 restart {shlex.quote(process_name)} --update-env || '
            f'pm2 start "{run_command}" --name {shlex.quote(process_name)}'
        )
        pm2_result = await DeploymentService._execute_remote(
            server=server,
            command=pm2_command,
            step="pm2_start_or_restart",
        )
        if pm2_result.exit_code != 0:
            raise DeploymentService._structured_command_error(
                step="pm2_start_or_restart",
                output=pm2_result.output,
                exit_code=pm2_result.exit_code,
            )

        verify_command = DeploymentService._deployment_command(
            workspace_path=workspace_path,
            port=port,
        ).replace(f"python3 -m http.server {port}", f"curl -fsS http://127.0.0.1:{port}")
        verify_result = await DeploymentService._execute_remote(
            server=server,
            command=verify_command,
            step="verify_http_server",
        )
        if verify_result.exit_code != 0:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail={
                    "message": "Deployment process started but HTTP server is not reachable",
                    "step": "verify_http_server",
                    "exit_code": verify_result.exit_code,
                    "output": (verify_result.output or "")[:2000],
                },
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
            "domain": workspace.get("domain"),
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
        return {
            "workspace_id": workspace_id,
            "port": deployment["port"],
            "domain": workspace.get("domain"),
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
