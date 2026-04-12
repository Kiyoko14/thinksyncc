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
    _MAX_UNIQUE_ATTEMPTS = 50

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
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"code": "WORKSPACE_NOT_FOUND"},
            )

    @staticmethod
    def _workspace_path(slug: str) -> str:
        # Deterministic project root for all workspaces.
        return f"/home/root/workspaces/{slug}"

    @staticmethod
    def _unique_slug(*, supabase, server_id: str, base_slug: str) -> str:
        base = (base_slug or "").strip().lower()
        if not base:
            base = "workspace"

        def exists(candidate: str) -> bool:
            result = (
                supabase.table("workspaces")
                .select("id")
                .eq("server_id", server_id)
                .eq("slug", candidate)
                .limit(1)
                .execute()
            )
            return bool(result.data)

        if not exists(base):
            return base

        for i in range(2, WorkspaceService._MAX_UNIQUE_ATTEMPTS + 1):
            candidate = f"{base}-{i}"
            if not exists(candidate):
                return candidate

        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Workspace slug already exists on this server",
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
        supabase = get_supabase()

        workspace_id = str(uuid4())
        base_slug = SlugService.generate_slug(cleaned_name)
        slug = WorkspaceService._unique_slug(supabase=supabase, server_id=server_id, base_slug=base_slug)
        domain = SlugService.generate_domain(slug=slug, workspace_id=workspace_id)
        workspace_path = WorkspaceService._workspace_path(slug)

        mkdir_command = f"mkdir -p {shlex.quote(workspace_path)}"
        await SSHService.execute(server=server, command=mkdir_command)

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
                    detail="Workspace slug already exists on this server",
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
        rows = result.data or []
        hydrated: list[WorkspaceResponse] = []
        for row in rows:
            repaired = WorkspaceService._ensure_workspace_fields(dict(row), supabase=supabase, user_id=user_id)
            hydrated.append(WorkspaceResponse(**repaired))
        return hydrated

    @staticmethod
    def get_workspace_by_id(id: str, user_id: str) -> dict[str, Any]:
        if not isinstance(id, str) or not id.strip():
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail={"code": "WORKSPACE_REQUIRED"})
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
                detail={"code": "WORKSPACE_NOT_FOUND"},
            )

        if not result or not result.data:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"code": "WORKSPACE_NOT_FOUND"},
            )

        row = dict(result.data)
        return WorkspaceService._ensure_workspace_fields(row, supabase=supabase, user_id=user_id)

    @staticmethod
    def _ensure_workspace_fields(
        row: dict[str, Any],
        *,
        supabase,
        user_id: str,
    ) -> dict[str, Any]:
        slug = (row.get("slug") or "").strip()
        domain = (row.get("domain") or "").strip()
        path = (row.get("path") or "").strip()

        if slug and domain and path:
            return row

        server_id = str(row.get("server_id") or "")
        workspace_id = str(row.get("id") or "")
        name = str(row.get("name") or "workspace")

        patch: dict[str, Any] = {}
        if not slug:
            base_slug = SlugService.generate_slug(name)
            slug = WorkspaceService._unique_slug(supabase=supabase, server_id=server_id, base_slug=base_slug)
            patch["slug"] = slug

        if not domain:
            domain = SlugService.generate_domain(slug=slug, workspace_id=workspace_id)
            patch["domain"] = domain

        if not path:
            path = WorkspaceService._workspace_path(slug)
            patch["path"] = path

        if patch:
            try:
                supabase.table("workspaces").update(patch).eq("id", workspace_id).execute()
            except Exception:
                # Do not block reads; caller still gets a consistent view.
                pass
            row.update(patch)

        return row
