from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from fastapi import HTTPException, status
from postgrest.exceptions import APIError

from core.database import get_supabase


class DeploymentService:
    """Manage workspace deployments and domain→port mappings."""

    _MIN_PORT = 10000
    _MAX_PORT = 65535

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
    def get_next_available_port() -> int:
        """Allocate next available port from pool."""
        supabase = get_supabase()
        try:
            # Find max port currently used
            result = (
                supabase.table("workspace_deployments")
                .select("port")
                .is_("is_active", "true")
                .order("port", desc=True)
                .limit(1)
                .execute()
            )
            if result.data:
                last_port = result.data[0]["port"]
                next_port = last_port + 1
                if next_port <= DeploymentService._MAX_PORT:
                    return next_port
        except APIError:
            pass

        return DeploymentService._MIN_PORT

    @staticmethod
    def create_deployment(workspace_id: str, user_id: str) -> dict[str, Any]:
        """Create a deployment for a workspace."""
        DeploymentService._validate_uuid(workspace_id, "workspace_id")
        DeploymentService._validate_uuid(user_id, "user_id")

        from services.workspace_service import WorkspaceService

        workspace = WorkspaceService.get_workspace_by_id(id=workspace_id, user_id=user_id)

        port = DeploymentService.get_next_available_port()

        supabase = get_supabase()
        payload = {
            "workspace_id": workspace_id,
            "port": port,
            "is_active": True,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }

        try:
            result = supabase.table("workspace_deployments").insert(payload).execute()
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

        if not result or not result.data:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to create deployment",
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
    def deactivate_deployment(workspace_id: str, user_id: str) -> None:
        """Deactivate workspace deployment."""
        DeploymentService._validate_uuid(workspace_id, "workspace_id")
        DeploymentService._validate_uuid(user_id, "user_id")

        from services.workspace_service import WorkspaceService

        workspace = WorkspaceService.get_workspace_by_id(id=workspace_id, user_id=user_id)

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
