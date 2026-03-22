from typing import Any

from fastapi import APIRouter, Depends

from core.security import get_current_user
from models.deployment import DeploymentResponse
from services.deployment_service import DeploymentService

router = APIRouter(prefix="/deployments", tags=["deployments"])


@router.post("/{workspace_id}", response_model=DeploymentResponse)
async def create_workspace_deployment(
    workspace_id: str,
    current_user: dict[str, Any] = Depends(get_current_user),
) -> DeploymentResponse:
    return DeploymentService.create_deployment(
        workspace_id=workspace_id,
        user_id=current_user["sub"],
    )


@router.get("/{workspace_id}", response_model=DeploymentResponse)
async def get_workspace_deployment(
    workspace_id: str,
    current_user: dict[str, Any] = Depends(get_current_user),
) -> DeploymentResponse:
    deployment = DeploymentService.get_deployment(
        workspace_id=workspace_id,
        user_id=current_user["sub"],
    )
    if not deployment:
        deployment = DeploymentService.create_deployment(
            workspace_id=workspace_id,
            user_id=current_user["sub"],
        )
    return DeploymentResponse(**deployment)


@router.delete("/{workspace_id}")
async def deactivate_workspace_deployment(
    workspace_id: str,
    current_user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, str]:
    DeploymentService.deactivate_deployment(
        workspace_id=workspace_id,
        user_id=current_user["sub"],
    )
    return {"message": "Deployment deactivated"}
