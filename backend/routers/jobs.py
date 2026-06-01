from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, status

from core.security import get_current_user
from models.job import JobAccepted, JobCreate, JobResponse
from services.agent_service import AgentService
from services import logger as obs
from services.job_recovery import JobRecovery
from services.job_queue import JobQueue
from services.workspace_service import WorkspaceService

router = APIRouter(prefix="/jobs", tags=["jobs"])


@router.post("/", response_model=JobAccepted, status_code=status.HTTP_202_ACCEPTED)
async def submit_job(
    payload: JobCreate,
    background_tasks: BackgroundTasks,
    current_user: dict[str, Any] = Depends(get_current_user),
) -> JobAccepted:
    """Submit a natural-language job. Returns immediately; poll or stream for results.

    Queue-based: the job is enqueued in the DB; workers claim and execute it.
    If no worker is running, the job is queued and will be picked up when a worker starts.
    """
    trace_id = obs.new_trace_id()
    if payload.allow_write is None:
        payload.allow_write = True
    user_id: str = current_user["sub"]
    accepted = AgentService.submit_job(user_id=user_id, payload=payload, trace_id=trace_id)
    # Enqueue the job in the durable queue (no BackgroundTasks)
    JobQueue.enqueue_job(accepted.id)
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
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail={"code": "WORKSPACE_REQUIRED"})
    WorkspaceService.get_workspace_by_id(id=workspace_id, user_id=current_user["sub"])
    job = AgentService.get_job(job_id=job_id, user_id=current_user["sub"])
    if job.workspace_id != workspace_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail={"code": "WORKSPACE_NOT_FOUND"})
    return job


@router.get("/{job_id}/timeline")
async def get_job_timeline(
    job_id: str,
    current_user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    """Return the complete execution timeline for a job.

    Reconstructs the execution from job_events, job_state_transitions,
    job_steps, job_decisions, and job_retries.
    """
    from services.execution_audit import ExecutionAudit
    job = AgentService.get_job(job_id=job_id, user_id=current_user["sub"])
    if not job:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail={"code": "JOB_NOT_FOUND"})
    timeline = ExecutionAudit.get_execution_timeline(job_id)
    return timeline


@router.get("/{job_id}/steps")
async def get_job_steps(
    job_id: str,
    current_user: dict[str, Any] = Depends(get_current_user),
) -> list[dict[str, Any]]:
    """Retrieve durable step records for a job."""
    from services.execution_repository import get_steps
    job = AgentService.get_job(job_id=job_id, user_id=current_user["sub"])
    if not job:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail={"code": "JOB_NOT_FOUND"})
    return get_steps(job_id)


@router.get("/{job_id}/decisions")
async def get_job_decisions(
    job_id: str,
    current_user: dict[str, Any] = Depends(get_current_user),
) -> list[dict[str, Any]]:
    """Retrieve durable decision records for a job."""
    from services.execution_repository import get_decisions
    job = AgentService.get_job(job_id=job_id, user_id=current_user["sub"])
    if not job:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail={"code": "JOB_NOT_FOUND"})
    return get_decisions(job_id)


@router.get("/{job_id}/retries")
async def get_job_retries(
    job_id: str,
    current_user: dict[str, Any] = Depends(get_current_user),
) -> list[dict[str, Any]]:
    """Retrieve durable retry records for a job."""
    from services.execution_repository import get_retries
    job = AgentService.get_job(job_id=job_id, user_id=current_user["sub"])
    if not job:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail={"code": "JOB_NOT_FOUND"})
    return get_retries(job_id)


@router.get("/{job_id}/errors")
async def get_job_errors(
    job_id: str,
    current_user: dict[str, Any] = Depends(get_current_user),
) -> list[dict[str, Any]]:
    """Retrieve durable error records for a job."""
    from services.execution_repository import get_execution_details
    job = AgentService.get_job(job_id=job_id, user_id=current_user["sub"])
    if not job:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail={"code": "JOB_NOT_FOUND"})
    return get_execution_details(job_id, detail_type="error")


# =============================================================================
# Recovery endpoints
# =============================================================================


@router.get("/recovery/report")
async def get_recovery_report(
    hours: int = Query(default=1, ge=1, le=72),
    current_user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    """Return a recovery report for the current user's jobs.

    Includes counts of unfinished, orphaned, and recoverable jobs.
    """
    report = JobRecovery.generate_recovery_report(hours=hours)
    return report


@router.post("/recovery/{job_id}/mark-recoverable")
async def mark_job_recoverable(
    job_id: str,
    current_user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    """Mark an orphaned job as recoverable and reset it to queued."""
    job = AgentService.get_job(job_id=job_id, user_id=current_user["sub"])
    if not job:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail={"code": "JOB_NOT_FOUND"})
    success = JobRecovery.mark_job_recoverable(job_id, reason="manual_recovery")
    return {"success": success, "job_id": job_id}


@router.post("/recovery/{job_id}/mark-orphaned")
async def mark_job_orphaned(
    job_id: str,
    current_user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    """Mark an orphaned job as failed."""
    job = AgentService.get_job(job_id=job_id, user_id=current_user["sub"])
    if not job:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail={"code": "JOB_NOT_FOUND"})
    success = JobRecovery.mark_job_orphaned(job_id, reason="manual_orphaned")
    return {"success": success, "job_id": job_id}
