from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, status

from core.security import get_current_user
from models.job import JobAccepted, JobCreate, JobResponse
from services.agent_service import AgentService

router = APIRouter(prefix="/jobs", tags=["jobs"])


@router.post("/", response_model=JobAccepted, status_code=status.HTTP_202_ACCEPTED)
async def submit_job(
    payload: JobCreate,
    background_tasks: BackgroundTasks,
    current_user: dict[str, Any] = Depends(get_current_user),
) -> JobAccepted:
    """Submit a natural-language job. Returns immediately; poll or stream for results."""
    user_id: str = current_user["sub"]
    accepted = AgentService.create_job(user_id=user_id, payload=payload)
    background_tasks.add_task(AgentService.run_job, accepted.id, payload, user_id)
    return accepted


@router.get("/", response_model=list[JobResponse])
async def list_jobs(
    current_user: dict[str, Any] = Depends(get_current_user),
) -> list[JobResponse]:
    """List all jobs for the authenticated user, newest first."""
    return AgentService.list_jobs(user_id=current_user["sub"])


@router.get("/{job_id}", response_model=JobResponse)
async def get_job(
    job_id: str,
    current_user: dict[str, Any] = Depends(get_current_user),
) -> JobResponse:
    """Fetch the current state of a job, including all executed steps."""
    return AgentService.get_job(job_id=job_id, user_id=current_user["sub"])
