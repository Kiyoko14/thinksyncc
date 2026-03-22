import re
import shlex
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse
from uuid import UUID

from fastapi import HTTPException, status
from postgrest.exceptions import APIError

from core.database import get_supabase
from models.git_repo import GitRepoResponse
from services.server_service import ServerService
from services.ssh_service import SSHService
from services.workspace_service import WorkspaceService


class GitService:
    _GITHUB_PATH_PATTERN = re.compile(r"^/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(?:\.git)?/?$")
    _BRANCH_PATTERN = re.compile(r"^[A-Za-z0-9._/-]{1,100}$")

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
    def _validate_github_url(url: str) -> str:
        cleaned = url.strip()
        if not cleaned:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Repository URL is required",
            )

        parsed = urlparse(cleaned)
        host = (parsed.hostname or "").lower()
        if parsed.scheme not in {"https", "http"} or host != "github.com":
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Only GitHub repository URLs are allowed",
            )

        if parsed.query or parsed.fragment:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Repository URL must not include query or fragment",
            )

        if not GitService._GITHUB_PATH_PATTERN.match(parsed.path):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid GitHub repository URL format",
            )

        # Normalize trailing slash for stable uniqueness checks.
        normalized_path = parsed.path.rstrip("/")
        return f"https://github.com{normalized_path}"

    @staticmethod
    def connect_repo(
        user_id: str,
        workspace_id: str,
        url: str,
        branch: str,
    ) -> GitRepoResponse:
        GitService._validate_uuid(user_id, "user_id")
        GitService._validate_uuid(workspace_id, "workspace_id")

        safe_url = GitService._validate_github_url(url)
        safe_branch = branch.strip()
        if not safe_branch:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Branch is required",
            )
        if not GitService._BRANCH_PATTERN.match(safe_branch):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid branch format",
            )

        WorkspaceService.get_workspace_by_id(id=workspace_id, user_id=user_id)
        workspace = WorkspaceService.get_workspace_by_id(id=workspace_id, user_id=user_id)

        supabase = get_supabase()
        payload = {
            "workspace_id": workspace_id,
            "user_id": user_id,
            "provider": "github",
            "url": safe_url,
            "branch": safe_branch,
            "path": workspace.get("path"),
            "created_at": datetime.now(timezone.utc).isoformat(),
        }

        try:
            result = supabase.table("git_repos").insert(payload).execute()
        except APIError as exc:
            code = GitService._api_error_code(exc)
            if code == "23505":
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="Repository already connected to this workspace",
                )
            if code in {"23503", "42501"}:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Access denied",
                )
            if code == "22P02":
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Invalid request data",
                )
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to connect repository",
            )

        if not result or not result.data:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to connect repository",
            )

        return GitRepoResponse(**result.data[0])

    @staticmethod
    def get_repos(user_id: str, workspace_id: str) -> list[GitRepoResponse]:
        GitService._validate_uuid(user_id, "user_id")
        GitService._validate_uuid(workspace_id, "workspace_id")

        WorkspaceService.get_workspace_by_id(id=workspace_id, user_id=user_id)

        supabase = get_supabase()
        result = (
            supabase.table("git_repos")
            .select("*")
            .eq("user_id", user_id)
            .eq("workspace_id", workspace_id)
            .order("created_at", desc=True)
            .execute()
        )

        return [GitRepoResponse(**row) for row in result.data]

    @staticmethod
    def get_repo_by_id(repo_id: str, user_id: str) -> dict[str, Any]:
        GitService._validate_uuid(repo_id, "repo_id")
        GitService._validate_uuid(user_id, "user_id")

        supabase = get_supabase()
        try:
            result = (
                supabase.table("git_repos")
                .select("*")
                .eq("id", repo_id)
                .eq("user_id", user_id)
                .maybe_single()
                .execute()
            )
        except APIError:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Repository not found",
            )

        if not result or not result.data:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Repository not found",
            )

        return result.data

    @staticmethod
    async def clone_repo(workspace: dict[str, Any], repo: dict[str, Any]) -> dict[str, str | bool]:
        workspace_id = str(workspace.get("id", ""))
        user_id = str(workspace.get("user_id", ""))
        repo_id = str(repo.get("id", ""))

        GitService._validate_uuid(workspace_id, "workspace_id")
        GitService._validate_uuid(user_id, "user_id")
        GitService._validate_uuid(repo_id, "repo_id")

        if bool(repo.get("is_cloned")):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Repository is already cloned",
            )

        workspace_path = str(workspace.get("path", "")).strip()
        if not workspace_path or ".." in workspace_path:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid workspace path",
            )

        server = ServerService.get_server(
            server_id=str(workspace.get("server_id", "")),
            user_id=user_id,
        )

        # Prevent nested repositories and duplicate clone in workspace root.
        git_dir_check = f"test -d {shlex.quote(workspace_path + '/.git')} && echo 1 || echo 0"
        git_dir_result = await SSHService.execute(server=server, command=git_dir_check)
        if git_dir_result.output.strip().endswith("1"):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Workspace already contains a Git repository",
            )

        clone_command = (
            f"cd {shlex.quote(workspace_path)} && "
            f"git clone --branch {shlex.quote(str(repo['branch']))} {shlex.quote(str(repo['url']))} ."
        )
        await SSHService.execute(server=server, command=clone_command)

        supabase = get_supabase()
        try:
            update_result = (
                supabase.table("git_repos")
                .update({"is_cloned": True})
                .eq("id", repo_id)
                .eq("user_id", user_id)
                .execute()
            )
        except APIError:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Repository cloned but failed to update status",
            )

        if not update_result or not update_result.data:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Repository cloned but failed to update status",
            )

        return {
            "repo_id": repo_id,
            "workspace_id": workspace_id,
            "is_cloned": True,
            "message": "Repository cloned successfully",
        }
