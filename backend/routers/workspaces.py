from typing import Any

from fastapi import APIRouter, Depends, Query, status

from core.security import get_current_user
from models.workspace import WorkspaceCreateRequest, WorkspaceResponse
from services.workspace_service import WorkspaceService

router = APIRouter(prefix="/workspaces", tags=["workspaces"])


@router.post("/", response_model=WorkspaceResponse, status_code=status.HTTP_201_CREATED)
async def add_workspace(
    payload: WorkspaceCreateRequest,
    current_user: dict[str, Any] = Depends(get_current_user),
) -> WorkspaceResponse:
    github_clone = None
    if payload.github_connection_id and payload.github_repo:
        from models.github import GitHubCloneRequest

        github_clone = GitHubCloneRequest(
            github_connection_id=payload.github_connection_id,
            repo=payload.github_repo,
            branch=payload.github_branch,
            depth=payload.github_depth,
        )
    return await WorkspaceService.create_workspace(
        user_id=current_user["sub"],
        server_id=payload.server_id,
        name=payload.name,
        github_connection_id=payload.github_connection_id,
        github_clone=github_clone,
    )


@router.get("/", response_model=list[WorkspaceResponse])
async def list_workspaces(
    server_id: str | None = Query(default=None),
    current_user: dict[str, Any] = Depends(get_current_user),
) -> list[WorkspaceResponse]:
    return WorkspaceService.get_workspaces(
        user_id=current_user["sub"],
        server_id=server_id,
    )


@router.get("/{workspace_id}", response_model=WorkspaceResponse)
async def get_workspace(
    workspace_id: str,
    current_user: dict[str, Any] = Depends(get_current_user),
) -> WorkspaceResponse:
    workspace = WorkspaceService.get_workspace_by_id(id=workspace_id, user_id=current_user["sub"])
    return WorkspaceResponse(**workspace)


@router.delete("/{workspace_id}")
async def delete_workspace(
    workspace_id: str,
    current_user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    """Delete a workspace through the lifecycle orchestrator (Part 4).

    Runs validate → execute → cleanup → audit → cache invalidation with
    compensation on failure. Removes the workspace, its remote directory, and
    an orphaned GitHub App connection (if this was the last workspace using it).
    """
    return await WorkspaceService.delete_workspace(
        workspace_id=workspace_id, user_id=current_user["sub"]
    )


@router.post("/{workspace_id}/disconnect")
async def disconnect_repository(
    workspace_id: str,
    current_user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    """Disconnect the GitHub repository from a workspace (Part 4).

    The workspace remains (reverts to a ThinkSync workspace); an orphaned App
    connection is cleaned up. Delegates to the lifecycle orchestrator.
    """
    return await WorkspaceService.disconnect_repository(
        workspace_id=workspace_id, user_id=current_user["sub"]
    )
