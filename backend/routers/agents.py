import asyncio
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, BackgroundTasks, Depends, WebSocket, WebSocketDisconnect

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
from services.emergent_agent_service import EmergentE1Service
from services.forge_v2 import ForgeV2Service

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


# ── Forge v1 async job queue endpoints ───────────────────────────────────────

@router.post("/forge/plan", response_model=AgentPlanResponse)
async def forge_plan(
    payload: AgentRunRequest,
    current_user: dict[str, Any] = Depends(get_current_user),
) -> AgentPlanResponse:
    """Return an execution plan without connecting to any server (instant)."""
    return await EmergentE1Service.get_plan(payload=payload, current_user=current_user)


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
    job_id = str(uuid4())
    ForgeV2Service.submit_job(job_id)
    background_tasks.add_task(ForgeV2Service.run_async, job_id, payload, current_user)
    return ForgeV2JobResponse(job_id=job_id, status=AgentJobStatus.QUEUED)


@router.get("/forge-v2/jobs/{job_id}", response_model=ForgeV2JobResponse)
async def forge_v2_job_status(
    job_id: str,
    current_user: dict[str, Any] = Depends(get_current_user),
) -> ForgeV2JobResponse:
    """Poll Forge v2 job status and retrieve results once completed."""
    return ForgeV2Service.get_job_status(job_id=job_id)


@router.websocket("/forge-v2/ws/{job_id}")
async def forge_v2_ws(job_id: str, websocket: WebSocket) -> None:
    """WebSocket endpoint for real-time Forge v2 job progress streaming.

    Clients connect after submitting a job via POST /forge-v2/run.
    Events are JSON objects: {"type": "...", ...}
    Connection closes automatically when a "completed" or "error" event is received.
    """
    await websocket.accept()
    try:
        events_queue = ForgeV2Service.get_events_queue(job_id)
    except Exception:
        await websocket.close(code=1008)
        return

    try:
        while True:
            try:
                event = await asyncio.wait_for(events_queue.get(), timeout=60.0)
            except asyncio.TimeoutError:
                await websocket.send_json({"type": "ping"})
                continue

            await websocket.send_json(event)

            if event.get("type") in ("completed", "error", "abort"):
                break
    except WebSocketDisconnect:
        pass
    finally:
        await websocket.close()


# ── Legacy alias (hidden from docs) ──────────────────────────────────────

@router.post("/e1/run", response_model=AgentRunResponse, include_in_schema=False)
async def run_emergent_e1_legacy(
    payload: AgentRunRequest,
    current_user: dict[str, Any] = Depends(get_current_user),
) -> AgentRunResponse:
    return await EmergentE1Service.run(payload=payload, current_user=current_user)
