import asyncio
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, WebSocket, WebSocketDisconnect, status

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
from services.emergent_agent_service import EmergentE1Service
from services.forge_v2 import ForgeV2Service
from services.workspace_service import WorkspaceService

router = APIRouter(prefix="/agents", tags=["agents"])


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

@router.post("/forge-v2/plan", response_model=ForgeV2PlanResponse)
async def forge_v2_plan(
    payload: ForgeV2RunRequest,
    current_user: dict[str, Any] = Depends(get_current_user),
) -> ForgeV2PlanResponse:
    """Generate an LLM-based execution plan without executing any steps."""
    return await ForgeV2Service.get_plan(payload=payload, current_user=current_user)


@router.post("/forge-v2/run", response_model=ForgeV2JobResponse, status_code=202)
async def forge_v2_run_async(
    payload: ForgeV2RunRequest,
    background_tasks: BackgroundTasks,
    current_user: dict[str, Any] = Depends(get_current_user),
) -> ForgeV2JobResponse:
    """Queue a Forge v2 agent job. Poll /forge-v2/jobs/{job_id} for results."""
    user_id: str = current_user["sub"]
    user_email = str(current_user.get("email", ""))
    ForgeV2Service._check_write_permission(user_email, payload)
    if payload.dry_run:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="dry_run is disabled for the production execution pipeline.",
        )
    if not payload.workspace_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "WORKSPACE_REQUIRED"},
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
    accepted = AgentService.submit_job(user_id=user_id, payload=job_payload)
    background_tasks.add_task(AgentService.run_job, accepted.id, job_payload, user_id)
    return {
        "job_id": accepted.id,
        "status": AgentJobStatus.QUEUED.value,
        "run": None,
        "error": None,
    }


@router.get("/forge-v2/jobs/{job_id}", response_model=ForgeV2JobResponse)
async def forge_v2_job_status(
    job_id: str,
    current_user: dict[str, Any] = Depends(get_current_user),
) -> ForgeV2JobResponse:
    """Poll Forge v2 job status and retrieve results once completed."""
    job = AgentService.get_job(job_id=job_id, user_id=current_user["sub"])
    if not job.workspace_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail={"code": "WORKSPACE_REQUIRED"})
    WorkspaceService.get_workspace_by_id(id=job.workspace_id, user_id=current_user["sub"])
    return to_forge_v2_response(job)


@router.websocket("/forge-v2/ws/{job_id}")
async def forge_v2_ws(job_id: str, websocket: WebSocket) -> None:
    await websocket.close(code=1008)


# ── Legacy alias (hidden from docs) ──────────────────────────────────────

@router.post("/e1/run", response_model=AgentRunResponse, include_in_schema=False)
async def run_emergent_e1_legacy(
    payload: AgentRunRequest,
    current_user: dict[str, Any] = Depends(get_current_user),
) -> AgentRunResponse:
    raise HTTPException(
        status_code=status.HTTP_410_GONE,
        detail="e1 is disabled. Use the unified forge-v2 pipeline.",
    )
