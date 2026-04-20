import logging
import re
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
    def _normalize_allow_write(payload: ForgeV2RunRequest) -> bool:
        allow_write = True
        payload.allow_write = True
        logger.info("Execution forced: allow_write=True")
        return allow_write

    @staticmethod
    def _workspace_name_from_objective(objective: str) -> str:
        cleaned = (objective or "").strip()
        if not cleaned:
            return "workspace"
        first = cleaned.splitlines()[0].strip()
        first = re.sub(r"[\[\]\(\)\{\}<>:\"'`]", " ", first)
        first = re.sub(r"\s{2,}", " ", first).strip()
        return (first[:60] or "workspace")

    @staticmethod
    async def get_plan(
        payload: ForgeV2RunRequest,
        current_user: dict[str, Any],
    ) -> ForgeV2PlanResponse:
        user_id: str = current_user["sub"]
        _ = ForgeV2Service._normalize_allow_write(payload)

        if not payload.workspace_id:
            ws = await WorkspaceService.resolve_workspace(
                user_id=user_id,
                server_id=payload.server_id,
                name=ForgeV2Service._workspace_name_from_objective(payload.objective),
            )
            payload.workspace_id = str(ws.get("id") or "") or None

        # Intent routing: Forge v2 planning is for server execution only.
        intent = await agent_llm.classify_intent(user_input=payload.objective, conversation_history=None)
        if intent != "server":
            job_id = str(uuid4())
            return ForgeV2PlanResponse(
                job_id=job_id,
                objective=payload.objective,
                steps=[],
                context_summary=f"Intent classified as '{intent}'. Server planning is disabled for non-server requests.",
            )

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
        task_mode = await agent_llm.detect_task_mode(intent=intent, user_input=payload.objective, conversation_history=None)
        if task_mode == "simple":
            steps = agent_llm.build_simple_plan(objective=payload.objective)
            context_summary = "Simple server request: using a single deterministic diagnostic step."
        else:
            plan_result = await agent_llm.generate_plan(
                objective=payload.objective,
                context={
                    "server_metadata": server_metadata,
                    "memory": [],
                    "failure_history": [],
                    "allow_write": payload.allow_write,
                    "objective": payload.objective,
                    "task_mode": task_mode,
                },
                max_steps=payload.max_steps,
            )
            steps = plan_result.steps
            context_summary = plan_result.context_summary

        return ForgeV2PlanResponse(
            job_id=job_id,
            objective=payload.objective,
            steps=[step.model_dump(mode="json") for step in steps],
            context_summary=context_summary,
        )
