from typing import Any

from fastapi import APIRouter, Depends, status

from core.security import get_current_user
from models.github import (
    GitHubCloneRequest,
    GitHubConnectionCreate,
    GitHubConnectionResponse,
    GitHubConnectionWithKey,
    GitHubRepoAccessRequest,
    GitHubRepoAccessResponse,
    GitHubRepoMetadata,
)
from services.github_service import GitHubService

router = APIRouter(prefix="/github-connections", tags=["github-connections"])


@router.post("/", response_model=GitHubConnectionResponse, status_code=status.HTTP_201_CREATED)
async def create_connection(
    payload: GitHubConnectionCreate,
    current_user: dict[str, Any] = Depends(get_current_user),
) -> GitHubConnectionResponse | GitHubConnectionWithKey:
    # The response may be GitHubConnectionWithKey (private key returned once).
    # FastAPI will serialize whichever model matches; we typed the function
    # return as the base and rely on field presence.
    return await GitHubService.create_connection(user_id=current_user["sub"], payload=payload)


@router.get("/", response_model=list[GitHubConnectionResponse])
async def list_connections(
    current_user: dict[str, Any] = Depends(get_current_user),
) -> list[GitHubConnectionResponse]:
    return await GitHubService.list_connections(user_id=current_user["sub"])


@router.get("/{connection_id}", response_model=GitHubConnectionResponse)
async def get_connection(
    connection_id: str,
    current_user: dict[str, Any] = Depends(get_current_user),
) -> GitHubConnectionResponse:
    return await GitHubService.get_connection(connection_id=connection_id, user_id=current_user["sub"])


@router.delete("/{connection_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_connection(
    connection_id: str,
    current_user: dict[str, Any] = Depends(get_current_user),
) -> None:
    await GitHubService.delete_connection(connection_id=connection_id, user_id=current_user["sub"])


@router.post("/{connection_id}/check-access", response_model=GitHubRepoAccessResponse)
async def check_repo_access(
    connection_id: str,
    payload: GitHubRepoAccessRequest,
    current_user: dict[str, Any] = Depends(get_current_user),
) -> GitHubRepoAccessResponse:
    return await GitHubService.check_repo_access(
        user_id=current_user["sub"], connection_id=connection_id, payload=payload
    )


@router.get("/{connection_id}/metadata", response_model=GitHubRepoMetadata)
async def repo_metadata(
    connection_id: str,
    repo: str,
    current_user: dict[str, Any] = Depends(get_current_user),
) -> GitHubRepoMetadata:
    return await GitHubService.get_repo_metadata(
        user_id=current_user["sub"], connection_id=connection_id, repo=repo
    )
