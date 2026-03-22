import re
import shlex
from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

from fastapi import HTTPException, status
from postgrest.exceptions import APIError

from core.database import get_supabase
from models.workspace import WorkspaceResponse
from services.server_service import ServerService
from services.slug_service import SlugService
from services.ssh_service import SSHService


class WorkspaceService:
    _SAFE_NAME_PATTERN = re.compile(r"[^a-z0-9-]+")

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
    def _slugify_workspace_name(name: str) -> str:
        normalized = name.strip().lower().replace("_", "-")
        normalized = WorkspaceService._SAFE_NAME_PATTERN.sub("-", normalized)
        normalized = normalized.strip("-")
        return normalized or "workspace"

    @staticmethod
    def _safe_workspace_path(username: str, workspace_id: str, workspace_name: str) -> str:
        safe_username = username.strip()
        if not safe_username or "/" in safe_username or ".." in safe_username:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid server SSH user",
            )

        slug = WorkspaceService._slugify_workspace_name(workspace_name)
        path = f"/home/{safe_username}/workspaces/{workspace_id}-{slug}"

        # Guard against path traversal and enforce expected root.
        if ".." in path or not path.startswith(f"/home/{safe_username}/workspaces/"):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid workspace path",
            )

        return path

    @staticmethod
    def _get_unique_slug(supabase, max_retries: int = 5) -> str:
        """Generate a unique slug with collision handling."""
        for attempt in range(max_retries):
            slug = SlugService.generate_slug("workspace")
            try:
                result = (
                    supabase.table("workspaces")
                    .select("id")
                    .eq("slug", slug)
                    .limit(1)
                    .execute()
                )
                if not result.data:
                    return slug
            except APIError:
                pass

        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to generate unique workspace identifier",
        )

    @staticmethod
    async def create_workspace(user_id: str, server_id: str, name: str) -> WorkspaceResponse:
        WorkspaceService._validate_uuid(user_id, "user_id")
        WorkspaceService._validate_uuid(server_id, "server_id")

        cleaned_name = name.strip()
        if not cleaned_name:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Workspace name is required",
            )

        server = ServerService.get_server(server_id=server_id, user_id=user_id)

        workspace_id = str(uuid4())
        workspace_path = WorkspaceService._safe_workspace_path(
            username=server["ssh_user"],
            workspace_id=workspace_id,
            workspace_name=cleaned_name,
        )

        mkdir_command = f"mkdir -p {shlex.quote(workspace_path)}"
        await SSHService.execute(server=server, command=mkdir_command)

        supabase = get_supabase()

        # Generate unique slug and domain.
        slug = SlugService.generate_slug(cleaned_name)
        domain = SlugService.generate_domain(slug)

        # Verify slug uniqueness
        for attempt in range(5):
            try:
                check_result = (
                    supabase.table("workspaces")
                    .select("id")
                    .eq("slug", slug)
                    .limit(1)
                    .execute()
                )
                if check_result.data:
                    slug = SlugService.generate_slug(cleaned_name)
                    domain = SlugService.generate_domain(slug)
                    continue
                break
            except APIError:
                pass

        record = {
            "id": workspace_id,
            "user_id": user_id,
            "server_id": server_id,
            "name": cleaned_name,
            "path": workspace_path,
            "slug": slug,
            "domain": domain,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }

        try:
            result = supabase.table("workspaces").insert(record).execute()
        except APIError as exc:
            code = WorkspaceService._api_error_code(exc)
            if code == "23505":
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="Slug or domain collision — please try again",
                )
            if code == "23503":
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Server not found or access denied",
                )
            if code == "22P02":
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Invalid request data",
                )
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to create workspace",
            )

        if not result or not result.data:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to create workspace",
            )

        return WorkspaceResponse(**result.data[0])

    @staticmethod
    def get_workspaces(user_id: str, server_id: str | None = None) -> list[WorkspaceResponse]:
        WorkspaceService._validate_uuid(user_id, "user_id")
        if server_id is not None:
            WorkspaceService._validate_uuid(server_id, "server_id")

        supabase = get_supabase()
        query = (
            supabase.table("workspaces")
            .select("*")
            .eq("user_id", user_id)
            .order("created_at", desc=True)
        )
        if server_id is not None:
            query = query.eq("server_id", server_id)

        result = query.execute()
        return [WorkspaceResponse(**row) for row in result.data]

    @staticmethod
    def get_workspace_by_id(id: str, user_id: str) -> dict[str, Any]:
        WorkspaceService._validate_uuid(id, "workspace_id")
        WorkspaceService._validate_uuid(user_id, "user_id")

        supabase = get_supabase()
        try:
            result = (
                supabase.table("workspaces")
                .select("*")
                .eq("id", id)
                .eq("user_id", user_id)
                .maybe_single()
                .execute()
            )
        except APIError:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Workspace not found",
            )

        if not result or not result.data:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Workspace not found",
            )

        return result.data
