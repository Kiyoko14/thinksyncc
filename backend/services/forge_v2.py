import logging
from typing import Any
from uuid import uuid4

from fastapi import HTTPException, status

from core.config import get_settings
from models.agent import (
    ForgeV2PlanResponse,
    ForgeV2RunRequest,
)
from services import agent_llm
from services.server_service import ServerService
from services.workspace_service import WorkspaceService

logger = logging.getLogger(__name__)


class ForgeV2Service:
    """Service layer for Forge v2 agent operations."""

    @staticmethod
    def _check_write_permission(user_email: str, payload: ForgeV2RunRequest) -> None:
        """Restrict allow_write mode to admin emails when admins are configured."""
        settings = get_settings()
        admins = {e.strip().lower() for e in settings.AGENT_ADMIN_EMAILS.split(",") if e.strip()}

        if payload.allow_write and admins and user_email.lower() not in admins:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Write-mode is restricted to agent admins",
            )

    @staticmethod
    async def get_plan(
        payload: ForgeV2RunRequest,
        current_user: dict[str, Any],
    ) -> ForgeV2PlanResponse:
        user_id: str = current_user["sub"]
        user_email = str(current_user.get("email", ""))
        ForgeV2Service._check_write_permission(user_email, payload)

        if not payload.workspace_id:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail={"code": "WORKSPACE_REQUIRED"})

        workspace = WorkspaceService.get_workspace_by_id(id=payload.workspace_id, user_id=user_id)
        server = ServerService.get_server(server_id=payload.server_id, user_id=user_id)
        if workspace.get("server_id") != payload.server_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="workspace_id does not belong to the provided server_id",
            )
        server_metadata = {
            "host": server.get("host"),
            "ssh_user": server.get("ssh_user"),
            "name": server.get("name"),
        }

        job_id = str(uuid4())
        plan_result = await agent_llm.generate_plan(
            objective=payload.objective,
            context={
                "server_metadata": server_metadata,
                "failure_history": [],
                "allow_write": payload.allow_write,
                "objective": payload.objective,
            },
            max_steps=payload.max_steps,
        )

        return ForgeV2PlanResponse(
            job_id=job_id,
            objective=payload.objective,
            steps=[step.model_dump(mode="json") for step in plan_result.steps],
            context_summary=plan_result.context_summary,
        )
