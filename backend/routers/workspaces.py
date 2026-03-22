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
    return await WorkspaceService.create_workspace(
        user_id=current_user["sub"],
        server_id=payload.server_id,
        name=payload.name,
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
