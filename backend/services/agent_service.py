"""ThinkSync v2 agent orchestrator.

Coordinates the full agent loop:
  generate_plan → execute_tool → evaluate_step → persist → broadcast

Job state is persisted to the Supabase ``jobs`` table after every step
(steps column is JSONB, updated in place so one row = full audit trail).
WebSocket events are broadcast via per-job asyncio.Queue.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from fastapi import HTTPException, status
from postgrest.exceptions import APIError

from core.config import get_settings
from core.database import get_supabase
from models.agent import AgentDecision, DecisionAction, AgentStep
from models.job import JobAccepted, JobCreate, JobResponse, JobStatus
from services import llm_service
from services.forge_v2 import _execute_tool  # reuse validated tool dispatch
from services.server_service import ServerService

logger = logging.getLogger(__name__)

_TABLE = "jobs"

# Maximum events buffered per job queue before new events are dropped.
_WS_QUEUE_MAXSIZE = 500

# Per-job WebSocket event queues.  Keyed by job UUID.
_queues: dict[str, asyncio.Queue[dict[str, Any]]] = {}

# Module-level concurrency guard.
_semaphore: asyncio.Semaphore | None = None


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _get_semaphore() -> asyncio.Semaphore:
    global _semaphore
    if _semaphore is None:
        settings = get_settings()
        _semaphore = asyncio.Semaphore(settings.AGENT_MAX_CONCURRENCY)
    return _semaphore


def _publish(job_id: str, event: dict[str, Any]) -> None:
    q = _queues.get(job_id)
    if q is not None:
        try:
            q.put_nowait(event)
        except asyncio.QueueFull:
            pass


def _db_update(job_id: str, patch: dict[str, Any]) -> None:
    """Persist a partial update to the jobs row; swallow non-fatal errors."""
    try:
        patch["updated_at"] = datetime.now(timezone.utc).isoformat()
        get_supabase().table(_TABLE).update(patch).eq("id", job_id).execute()
    except APIError as exc:
        logger.warning("jobs UPDATE failed (job=%s): %s", job_id, exc)


def _row_to_response(row: dict[str, Any]) -> JobResponse:
    return JobResponse(
        id=row["id"],
        user_id=row["user_id"],
        server_id=row["server_id"],
        objective=row["objective"],
        status=JobStatus(row["status"]),
        steps=row.get("steps") or [],
        summary=row.get("summary"),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


# ---------------------------------------------------------------------------
# Core agent loop
# ---------------------------------------------------------------------------


async def _run_agent_loop(
    *,
    job_id: str,
    payload: JobCreate,
    user_id: str,
) -> None:
    settings = get_settings()
    max_retries = settings.AGENT_MAX_RETRIES
    step_timeout = payload.step_timeout_seconds or settings.AGENT_STEP_TIMEOUT

    # ── Fetch server ────────────────────────────────────────────────────────
    _db_update(job_id, {"status": JobStatus.RUNNING.value})
    _publish(job_id, {"type": "status", "status": JobStatus.RUNNING.value})

    try:
        server = ServerService.get_server(server_id=payload.server_id, user_id=user_id)
    except HTTPException as exc:
        _db_update(job_id, {"status": JobStatus.FAILED.value, "summary": exc.detail})
        _publish(job_id, {"type": "error", "detail": exc.detail})
        return

    # ── Generate plan ────────────────────────────────────────────────────────
    _db_update(job_id, {"status": JobStatus.WAITING_FOR_LLM.value})
    _publish(job_id, {"type": "status", "status": JobStatus.WAITING_FOR_LLM.value})

    server_metadata = {
        "host": server.get("host"),
        "ssh_user": server.get("ssh_user"),
        "name": server.get("name"),
    }
    context: dict[str, Any] = {
        "server_metadata": server_metadata,
        "failure_history": [],
        "allow_write": payload.allow_write,
        "objective": payload.objective,
    }

    try:
        plan = await llm_service.generate_plan(
            objective=payload.objective,
            context=context,
            max_steps=payload.max_steps,
        )
    except HTTPException as exc:
        _db_update(job_id, {"status": JobStatus.FAILED.value, "summary": exc.detail})
        _publish(job_id, {"type": "error", "detail": exc.detail})
        return

    _db_update(job_id, {"status": JobStatus.RUNNING.value})
    _publish(job_id, {"type": "plan", "steps": [s.model_dump() for s in plan.steps]})

    # ── Execute loop ─────────────────────────────────────────────────────────
    persisted_steps: list[dict[str, Any]] = []
    summary_parts: list[str] = []
    final_success = True
    remaining: list[AgentStep] = list(plan.steps)
    step_index = 0

    while step_index < len(remaining):
        step = remaining[step_index]
        retry_count = 0
        step_done = False

        while not step_done:
            _publish(job_id, {"type": "step_start", "step": step.step, "tool": step.tool.value})

            result = await _execute_tool(
                step=step,
                server=server,
                allow_write=payload.allow_write,
                timeout=step_timeout,
            )

            _publish(
                job_id,
                {
                    "type": "step_result",
                    "step": result.step,
                    "success": result.success,
                    "exit_code": result.exit_code,
                },
            )

            # ── LLM evaluation ───────────────────────────────────────────────
            _db_update(job_id, {"status": JobStatus.WAITING_FOR_LLM.value})
            _publish(job_id, {"type": "status", "status": JobStatus.WAITING_FOR_LLM.value})

            eval_context: dict[str, Any] = {
                **context,
                "previous_steps_summary": " | ".join(summary_parts),
                "retry_count": retry_count,
                "max_retries": max_retries,
            }

            try:
                decision: AgentDecision = await llm_service.evaluate_step(
                    step=step,
                    result=result,
                    context=eval_context,
                )
            except HTTPException as exc:
                decision = AgentDecision(
                    action=DecisionAction.ABORT,
                    reason=str(exc.detail),
                    summary_so_far="",
                )

            _db_update(job_id, {"status": JobStatus.RUNNING.value})
            _publish(job_id, {"type": "decision", "step": step.step, "action": decision.action.value})

            # ── Persist step to jobs.steps JSONB ─────────────────────────────
            step_record: dict[str, Any] = {
                "step": result.step,
                "tool": result.tool.value,
                "args": result.args,
                "stdout": result.stdout[:4000],
                "stderr": result.stderr[:2000],
                "exit_code": result.exit_code,
                "duration_ms": result.duration_ms,
                "success": result.success,
                "decision": decision.action.value,
                "decision_reason": decision.reason[:500],
                "executed_at": result.executed_at.isoformat(),
            }
            persisted_steps.append(step_record)
            _db_update(job_id, {"steps": persisted_steps})

            if decision.summary_so_far:
                summary_parts.append(decision.summary_so_far)

            if decision.action == DecisionAction.CONTINUE:
                step_done = True
                step_index += 1

            elif decision.action == DecisionAction.RETRY:
                if retry_count >= max_retries:
                    final_success = False
                    _publish(job_id, {"type": "abort", "step": step.step, "reason": "max retries exceeded"})
                    step_index = len(remaining)
                    step_done = True
                else:
                    retry_count += 1
                    context["failure_history"].append(
                        {"step": step.step, "exit_code": result.exit_code, "stderr": result.stderr[:200]}
                    )

            elif decision.action == DecisionAction.MODIFY:
                if decision.modified_step:
                    remaining[step_index] = decision.modified_step
                    step = decision.modified_step
                    retry_count += 1
                    if retry_count > max_retries:
                        final_success = False
                        step_index = len(remaining)
                        step_done = True
                else:
                    final_success = False
                    step_index = len(remaining)
                    step_done = True

            else:  # ABORT or unknown
                final_success = False
                _publish(job_id, {"type": "abort", "step": step.step, "reason": decision.reason})
                step_index = len(remaining)
                step_done = True

    ok_count = sum(1 for s in persisted_steps if s.get("success"))
    summary = (
        f"Completed: {len(persisted_steps)} step(s) executed, {ok_count} successful."
        + (" " + " | ".join(summary_parts) if summary_parts else "")
    ).strip()

    final_status = JobStatus.COMPLETED if final_success else JobStatus.FAILED
    _db_update(job_id, {"status": final_status.value, "summary": summary, "steps": persisted_steps})
    _publish(job_id, {"type": "completed", "success": final_success, "summary": summary})


# ---------------------------------------------------------------------------
# Public service API
# ---------------------------------------------------------------------------


class AgentService:
    """Service layer for ThinkSync v2 job management."""

    @staticmethod
    def create_job(user_id: str, payload: JobCreate) -> JobAccepted:
        """Insert a job row in Supabase and register an event queue."""
        job_id = str(uuid4())
        now = datetime.now(timezone.utc).isoformat()
        record: dict[str, Any] = {
            "id": job_id,
            "user_id": user_id,
            "server_id": payload.server_id,
            "objective": payload.objective,
            "status": JobStatus.QUEUED.value,
            "steps": [],
            "summary": None,
            "created_at": now,
            "updated_at": now,
        }

        try:
            result = get_supabase().table(_TABLE).insert(record).execute()
        except APIError as exc:
            logger.error("Failed to insert job row: %s", exc)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to create job",
            )

        if not result or not result.data:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to create job",
            )

        _queues[job_id] = asyncio.Queue(maxsize=_WS_QUEUE_MAXSIZE)
        return JobAccepted(id=job_id)

    @staticmethod
    def get_job(job_id: str, user_id: str) -> JobResponse:
        """Fetch a single job owned by the user."""
        try:
            result = (
                get_supabase()
                .table(_TABLE)
                .select("*")
                .eq("id", job_id)
                .eq("user_id", user_id)
                .maybe_single()
                .execute()
            )
        except APIError:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Job not found",
            )

        if not result or not result.data:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Job not found",
            )

        return _row_to_response(result.data)

    @staticmethod
    def list_jobs(user_id: str) -> list[JobResponse]:
        """Return all jobs for the authenticated user, newest first."""
        result = (
            get_supabase()
            .table(_TABLE)
            .select("*")
            .eq("user_id", user_id)
            .order("created_at", desc=True)
            .execute()
        )
        return [_row_to_response(row) for row in result.data or []]

    @staticmethod
    def get_events_queue(job_id: str) -> asyncio.Queue[dict[str, Any]]:
        """Return the in-memory event queue for a running job."""
        q = _queues.get(job_id)
        if q is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Job not found or not streaming",
            )
        return q

    @staticmethod
    async def run_job(
        job_id: str,
        payload: JobCreate,
        user_id: str,
    ) -> None:
        """Background coroutine: acquire semaphore, run agent loop."""
        sem = _get_semaphore()
        async with sem:
            try:
                await _run_agent_loop(job_id=job_id, payload=payload, user_id=user_id)
            except Exception as exc:
                logger.exception("Unhandled error in agent loop (job=%s): %s", job_id, exc)
                _db_update(job_id, {"status": JobStatus.FAILED.value, "summary": str(exc)})
                _publish(job_id, {"type": "error", "detail": str(exc)})
