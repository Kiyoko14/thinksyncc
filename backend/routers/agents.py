import logging
import asyncio
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, WebSocket, WebSocketDisconnect, status
from pydantic import BaseModel, Field

from core.security import get_current_user
from models.agent import (
    AgentAsyncRunAccepted,
    AgentJobResponse,
    AgentJobStatus,
    AgentOrchestrationRequest,
    AgentOrchestrationResponse,
    AgentPlanResponse,
    AgentRunRequest,
    AgentRunResponse,
    ForgeV2JobResponse,
    ForgeV2PlanResponse,
    ForgeV2RunRequest,
)
from models.job import JobCreate
from services.agent_service import AgentService, to_forge_v2_response
from services import logger as obs
from services.workspace_service import WorkspaceService

router = APIRouter(prefix="/agents", tags=["agents"])
logger = logging.getLogger(__name__)


class ExplicitModeRouteRequest(BaseModel):
    mode: str | None = None
    objective: str = Field(..., min_length=3, max_length=1000)
    server_id: str | None = None
    workspace_id: str | None = None
    max_steps: int = Field(default=8, ge=1, le=20)
    allow_write: bool | None = None
    step_timeout_seconds: int | None = Field(default=None, ge=5, le=600)


# ── Synchronous endpoints ──────────────────────────────────────────────────

@router.post("/forge-v1/run", response_model=AgentRunResponse)
async def run_forge_v1(
    payload: AgentRunRequest,
    current_user: dict[str, Any] = Depends(get_current_user),
) -> AgentRunResponse:
    raise HTTPException(
        status_code=status.HTTP_410_GONE,
        detail="forge-v1 is disabled. Use the unified forge-v2 pipeline.",
    )


@router.post("/forge-v1/orchestrate", response_model=AgentOrchestrationResponse)
async def orchestrate_forge_v1(
    payload: AgentOrchestrationRequest,
    current_user: dict[str, Any] = Depends(get_current_user),
) -> AgentOrchestrationResponse:
    raise HTTPException(
        status_code=status.HTTP_410_GONE,
        detail="forge-v1 orchestration is disabled. Use the unified forge-v2 pipeline.",
    )


# ── Forge v1 async job queue endpoints ───────────────────────────────────────

@router.post("/forge/plan", response_model=AgentPlanResponse)
async def forge_plan(
    payload: AgentRunRequest,
    current_user: dict[str, Any] = Depends(get_current_user),
) -> AgentPlanResponse:
    raise HTTPException(
        status_code=status.HTTP_410_GONE,
        detail="forge-v1 planning is disabled. Use /agents/forge-v2/plan.",
    )


@router.post("/forge/run", response_model=AgentAsyncRunAccepted, status_code=202)
async def forge_run_async(
    payload: AgentRunRequest,
    background_tasks: BackgroundTasks,
    current_user: dict[str, Any] = Depends(get_current_user),
) -> AgentAsyncRunAccepted:
    raise HTTPException(
        status_code=status.HTTP_410_GONE,
        detail="forge-v1 async jobs are disabled. Use the unified forge-v2 pipeline.",
    )


@router.get("/forge/jobs/{job_id}", response_model=AgentJobResponse)
async def forge_job_status(
    job_id: str,
    current_user: dict[str, Any] = Depends(get_current_user),
) -> AgentJobResponse:
    raise HTTPException(
        status_code=status.HTTP_410_GONE,
        detail="forge-v1 async jobs are disabled. Use the unified forge-v2 pipeline.",
    )


# ── Forge v2 endpoints ───────────────────────────────────────────────────────

@router.post("/forge-v2/plan")
async def forge_v2_plan(
    payload: ForgeV2RunRequest,
    current_user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    """Generate an LLM-based execution plan without executing any steps."""
    logger.info("[agents] forge_v2_plan request | user_id=%s | payload=%s", current_user.get("sub"), payload.model_dump(mode="json"))
    if payload.allow_write is None:
        payload.allow_write = True
    result = await ForgeV2Service.get_plan(payload=payload, current_user=current_user)
    data = result.model_dump(mode="json")
    logger.info("[agents] forge_v2_plan response | job_id=%s | steps=%s", data.get("job_id"), len(data.get("plan") or []))
    return {"status": "success", "data": data}


@router.post("/forge-v2/run", status_code=202)
async def forge_v2_run_async(
    payload: ForgeV2RunRequest,
    background_tasks: BackgroundTasks,
    current_user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    """Queue a Forge v2 agent job. Poll /forge-v2/jobs/{job_id} for results."""
    trace_id = obs.new_trace_id()
    user_id: str = current_user["sub"]
    logger.info("[agents] forge_v2_run request | trace_id=%s | user_id=%s | payload=%s", trace_id, user_id, payload.model_dump(mode="json"))
    if payload.allow_write is None:
        payload.allow_write = True
    if payload.dry_run:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="dry_run is disabled for the production execution pipeline.",
        )

    job_payload = JobCreate(
        workspace_id=payload.workspace_id,
        server_id=payload.server_id,
        objective=payload.objective,
        max_steps=payload.max_steps,
        allow_write=payload.allow_write,
        dry_run=False,
        step_timeout_seconds=payload.step_timeout_seconds,
    )
    accepted = AgentService.submit_job(user_id=user_id, payload=job_payload, trace_id=trace_id)
    # Enqueue in durable queue (no BackgroundTasks)
    from services.job_queue import JobQueue
    JobQueue.enqueue_job(accepted.id)
    data = {
        "job_id": accepted.id,
        "status": AgentJobStatus.QUEUED.value,
        "run": None,
        "error": None,
    }
    logger.info("[agents] forge_v2_run accepted | trace_id=%s | job_id=%s", trace_id, accepted.id)
    return {"status": "success", "data": data}


@router.get("/forge-v2/jobs/{job_id}")
async def forge_v2_job_status(
    job_id: str,
    current_user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    """Poll Forge v2 job status and retrieve results once completed."""
    logger.info("[agents] forge_v2_job_status request | user_id=%s | job_id=%s", current_user.get("sub"), job_id)
    job = AgentService.get_job(job_id=job_id, user_id=current_user["sub"])
    if not job.workspace_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail={"code": "WORKSPACE_REQUIRED"})
    WorkspaceService.get_workspace_by_id(id=job.workspace_id, user_id=current_user["sub"])
    data = to_forge_v2_response(job)
    logger.info("[agents] forge_v2_job_status response | job_id=%s | status=%s | error=%s", job_id, data.get("status"), data.get("error"))
    return {"status": "success", "data": data}


@router.websocket("/forge-v2/ws/{job_id}")
async def forge_v2_ws(job_id: str, websocket: WebSocket) -> None:
    await websocket.close(code=1008)


# ── Legacy alias (hidden from docs) ──────────────────────────────────────


@router.post("/route")
async def explicit_intent_route(
    payload: ExplicitModeRouteRequest,
    current_user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    trace_id = obs.new_trace_id()
    mode = (payload.mode or "").strip().lower()
    if not mode:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail={"error": "mode_required"})

    obs.emit(level="INFO", layer="router", message="explicit_route_selected", trace_id=trace_id, meta={"mode": mode, "user_id": current_user.get("sub")})

    logger.info("[agents] explicit_route request | trace_id=%s | user_id=%s | payload=%s", trace_id, current_user.get("sub"), payload.model_dump(mode="json"))
    result = await AgentService.run_explicit_mode(
        user_id=current_user["sub"],
        mode=mode,
        objective=payload.objective,
        server_id=payload.server_id,
        workspace_id=payload.workspace_id,
        max_steps=payload.max_steps,
        allow_write=payload.allow_write,
        step_timeout_seconds=payload.step_timeout_seconds,
        trace_id=trace_id,
    )
    logger.info("[agents] explicit_route response | trace_id=%s | result=%s", trace_id, result)
    return {"status": "success", "data": result}
