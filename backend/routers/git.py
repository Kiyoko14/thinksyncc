from typing import Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from core.security import get_current_user
from models.git_repo import GitConnectRequest, GitRepoResponse
from services.git_service import GitService
from services.workspace_service import WorkspaceService

router = APIRouter(prefix="/git", tags=["git"])


class GitCloneResponse(BaseModel):
    repo_id: str
    workspace_id: str
    is_cloned: bool
    message: str


@router.post("/connect", response_model=GitRepoResponse)
async def connect_repository(
    payload: GitConnectRequest,
    current_user: dict[str, Any] = Depends(get_current_user),
) -> GitRepoResponse:
    return GitService.connect_repo(
        user_id=current_user["sub"],
        workspace_id=payload.workspace_id,
        url=payload.url,
        branch=payload.branch,
    )


@router.get("/", response_model=list[GitRepoResponse])
async def list_repositories(
    workspace_id: str = Query(...),
    current_user: dict[str, Any] = Depends(get_current_user),
) -> list[GitRepoResponse]:
    return GitService.get_repos(
        user_id=current_user["sub"],
        workspace_id=workspace_id,
    )


@router.post("/{repo_id}/clone", response_model=GitCloneResponse)
async def clone_repository(
    repo_id: str,
    current_user: dict[str, Any] = Depends(get_current_user),
) -> GitCloneResponse:
    repo = GitService.get_repo_by_id(repo_id=repo_id, user_id=current_user["sub"])
    workspace = WorkspaceService.get_workspace_by_id(
        id=str(repo["workspace_id"]),
        user_id=current_user["sub"],
    )

    cloned = await GitService.clone_repo(workspace=workspace, repo=repo)
    return GitCloneResponse(**cloned)
