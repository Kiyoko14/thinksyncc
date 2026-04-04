from typing import Any
from uuid import uuid4

from fastapi import APIRouter, BackgroundTasks, Depends

from core.security import get_current_user
from models.agent import (
    AgentAsyncRunAccepted,
    AgentJobResponse,
    AgentOrchestrationRequest,
    AgentOrchestrationResponse,
    AgentPlanResponse,
    AgentRunRequest,
    AgentRunResponse,
)
from services.emergent_agent_service import EmergentE1Service

router = APIRouter(prefix="/agents", tags=["agents"])


# ── Synchronous endpoints ──────────────────────────────────────────────────

@router.post("/forge-v1/run", response_model=AgentRunResponse)
async def run_forge_v1(
    payload: AgentRunRequest,
    current_user: dict[str, Any] = Depends(get_current_user),
) -> AgentRunResponse:
    return await EmergentE1Service.run(payload=payload, current_user=current_user)


@router.post("/forge-v1/orchestrate", response_model=AgentOrchestrationResponse)
async def orchestrate_forge_v1(
    payload: AgentOrchestrationRequest,
    current_user: dict[str, Any] = Depends(get_current_user),
) -> AgentOrchestrationResponse:
    return await EmergentE1Service.orchestrate(payload=payload, current_user=current_user)


# ── Async job queue endpoints ─────────────────────────────────────────────

@router.post("/forge/plan", response_model=AgentPlanResponse)
async def forge_plan(
    payload: AgentRunRequest,
    current_user: dict[str, Any] = Depends(get_current_user),
) -> AgentPlanResponse:
    """Return an execution plan without connecting to any server (instant)."""
    return EmergentE1Service.get_plan(payload=payload, current_user=current_user)


@router.post("/forge/run", response_model=AgentAsyncRunAccepted, status_code=202)
async def forge_run_async(
    payload: AgentRunRequest,
    background_tasks: BackgroundTasks,
    current_user: dict[str, Any] = Depends(get_current_user),
) -> AgentAsyncRunAccepted:
    """Queue the agent job and return immediately. Poll /forge/jobs/{job_id} for results."""
    job_id = str(uuid4())
    EmergentE1Service.submit_job(job_id)
    background_tasks.add_task(EmergentE1Service._run_job, job_id, payload, current_user)
    return AgentAsyncRunAccepted(job_id=job_id)


@router.get("/forge/jobs/{job_id}", response_model=AgentJobResponse)
async def forge_job_status(
    job_id: str,
    current_user: dict[str, Any] = Depends(get_current_user),
) -> AgentJobResponse:
    """Poll job status and retrieve results once completed."""
    return EmergentE1Service.get_job_status(job_id=job_id)


# ── Legacy alias (hidden from docs) ──────────────────────────────────────

@router.post("/e1/run", response_model=AgentRunResponse, include_in_schema=False)
async def run_emergent_e1_legacy(
    payload: AgentRunRequest,
    current_user: dict[str, Any] = Depends(get_current_user),
) -> AgentRunResponse:
    return await EmergentE1Service.run(payload=payload, current_user=current_user)
