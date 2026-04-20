from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, Query, status

from core.security import get_current_user
from models.job import JobAccepted, JobCreate, JobResponse
from services.agent_service import AgentService
from services import logger as obs
from services.workspace_service import WorkspaceService

router = APIRouter(prefix="/jobs", tags=["jobs"])


@router.post("/", response_model=JobAccepted, status_code=status.HTTP_202_ACCEPTED)
async def submit_job(
    payload: JobCreate,
    background_tasks: BackgroundTasks,
    current_user: dict[str, Any] = Depends(get_current_user),
) -> JobAccepted:
    """Submit a natural-language job. Returns immediately; poll or stream for results."""
    trace_id = obs.new_trace_id()
    if payload.allow_write is None:
        payload.allow_write = True
    user_id: str = current_user["sub"]
    accepted = AgentService.submit_job(user_id=user_id, payload=payload, trace_id=trace_id)
    background_tasks.add_task(AgentService.run_job, accepted.id, payload, user_id, trace_id=trace_id)
    return accepted


@router.get("/", response_model=list[JobResponse])
async def list_jobs(
    workspace_id: str | None = Query(default=None),
    current_user: dict[str, Any] = Depends(get_current_user),
) -> list[JobResponse]:
    """List all jobs for the authenticated user, newest first."""
    return AgentService.list_jobs(user_id=current_user["sub"], workspace_id=workspace_id)


@router.get("/{job_id}", response_model=JobResponse)
async def get_job(
    job_id: str,
    workspace_id: str | None = Query(default=None),
    current_user: dict[str, Any] = Depends(get_current_user),
) -> JobResponse:
    """Fetch the current state of a job, including all executed steps."""
    if not workspace_id:
        from fastapi import HTTPException
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail={"code": "WORKSPACE_REQUIRED"})
    WorkspaceService.get_workspace_by_id(id=workspace_id, user_id=current_user["sub"])
    job = AgentService.get_job(job_id=job_id, user_id=current_user["sub"])
    if job.workspace_id != workspace_id:
        from fastapi import HTTPException
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail={"code": "WORKSPACE_NOT_FOUND"})
    return job
