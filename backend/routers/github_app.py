"""GitHub App OAuth + installation REST endpoints (production-ready, Phase 1).

Frontend flow (Phase 2 will consume these):
    Connect GitHub  -> GET  /github-app/authorize   (browser redirect)
    GitHub Authorization -> GitHub redirects to GITHUB_APP_REDIRECT_URI
    Return to ThinkSync   -> GET  /github-app/callback  (303 -> frontend)
    Repository List       -> GET  /github-app/installations/{id}/repositories
    Select Repository     -> POST /github-app/workspaces  (clone + auto workspace)
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query, Request, status
from fastapi.responses import RedirectResponse

from core.security import get_current_user
from models.github_app import (
    GitHubAppAuthorizeResponse,
    GitHubAppCallbackRequest,
    GitHubAppCloneRequest,
    GitHubAppTokenResponse,
    GitHubAppWorkspaceRequest,
    GitHubRepositoryListResponse,
    GitHubRepositorySelectRequest,
)
from models.workspace import WorkspaceResponse
from services.github_app_service import GitHubAppService

router = APIRouter(prefix="/github-app", tags=["github-app"])


@router.get("/authorize", response_model=GitHubAppAuthorizeResponse)
async def authorize(
    current_user: dict[str, Any] = Depends(get_current_user),
) -> GitHubAppAuthorizeResponse:
    return await GitHubAppService.authorize_url(user_id=current_user["sub"])


@router.get("/callback", status_code=status.HTTP_303_SEE_OTHER)
async def callback(
    request: Request,
    code: str = Query(...),
    state: str = Query(...),
    current_user: dict[str, Any] = Depends(get_current_user),
) -> Any:
    """Complete the OAuth handshake, then redirect the browser back to the app.

    Returns 303 See Other to the frontend callback route (the production
    redirect target is ``/github/callback``). The frontend reads the result
    and continues to the repository list.
    """
    result = await GitHubAppService.handle_callback(
        user_id=current_user["sub"], code=code, state=state
    )
    from core.config import get_settings

    settings = get_settings()
    # Redirect back into the SPA. Pass the installation id + account as query.
    target = (
        f"{settings.GITHUB_APP_REDIRECT_URI.split('/github/callback')[0]}/github/callback"
        f"?installation_id={result.installation_id}"
        f"&account={result.github_account_login}"
    )
    return RedirectResponse(url=target, status_code=status.HTTP_303_SEE_OTHER)


@router.get("/installations")
async def list_installations(
    current_user: dict[str, Any] = Depends(get_current_user),
):
    return await GitHubAppService.list_installations(user_id=current_user["sub"])


@router.get(
    "/installations/{installation_id}/repositories",
    response_model=GitHubRepositoryListResponse,
)
async def list_repositories(
    installation_id: str,
    current_user: dict[str, Any] = Depends(get_current_user),
) -> GitHubRepositoryListResponse:
    return await GitHubAppService.list_repositories(
        user_id=current_user["sub"], installation_id=installation_id
    )


@router.post("/workspaces", response_model=WorkspaceResponse, status_code=status.HTTP_201_CREATED)
async def create_workspace(
    payload: GitHubAppWorkspaceRequest,
    current_user: dict[str, Any] = Depends(get_current_user),
) -> WorkspaceResponse:
    """Clone the selected repo (via App token) and auto-create the workspace.

    The workspace name is derived from the repository name. On success the
    frontend navigates the user straight to the Agent Chat for the new
    workspace (navigation is a frontend concern in Phase 2).
    """
    return await GitHubAppService.create_workspace_from_repo(
        user_id=current_user["sub"], payload=payload
    )
