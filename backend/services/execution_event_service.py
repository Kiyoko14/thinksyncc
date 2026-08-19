"""ExecutionEventService: centralized, durable execution event tracking.

Ensures every important execution event is persisted to the job_events table
with proper sequencing.  This is the canonical source for reconstructing
execution timelines.

All events are best-effort:  failures are logged but never raise exceptions.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

from core.config import get_settings
from core.database import get_supabase, get_supabase_async
from services.redis_service import RedisService

logger = logging.getLogger(__name__)

_LOCAL_SEQ: dict[str, int] = {}

EVENT_JOB_CREATED = "job_created"
EVENT_PLANNING_STARTED = "planning_started"
EVENT_PLANNING_COMPLETED = "planning_completed"
EVENT_EXECUTION_STARTED = "execution_started"
EVENT_STEP_STARTED = "step_started"
EVENT_STEP_COMPLETED = "step_completed"
EVENT_VALIDATION_STARTED = "validation_started"
EVENT_VALIDATION_COMPLETED = "validation_completed"
EVENT_RETRY_STARTED = "retry_started"
EVENT_RETRY_COMPLETED = "retry_completed"
EVENT_EXECUTION_FAILED = "execution_failed"
EVENT_EXECUTION_COMPLETED = "execution_completed"

# Worker events
EVENT_WORKER_CLAIMED = "worker_claimed"
EVENT_WORKER_HEARTBEAT = "worker_heartbeat"
EVENT_WORKER_RELEASED = "worker_released"
EVENT_WORKER_FAILED = "worker_failed"
EVENT_WORKER_COMPLETED = "worker_completed"
EVENT_WORKER_ABANDONED = "worker_abandoned"


class ExecutionEventService:
    """Durable execution event tracking with Redis and DB persistence."""

    @staticmethod
    async def _next_sequence(job_id: str) -> int:
        redis = RedisService.get_async_client()
        if redis is not None:
            try:
                return int(await redis.incr(f"job_events:{job_id}:seq"))
            except Exception as exc:
                logger.warning("Redis INCR failed for job=%s: %s", job_id, exc)
        _LOCAL_SEQ[job_id] = _LOCAL_SEQ.get(job_id, 0) + 1
        return _LOCAL_SEQ[job_id]

    @staticmethod
    async def emit(
        job_id: str,
        event_type: str,
        payload: dict[str, Any],
        *,
        workspace_id: str | None = None,
        trace_id: str | None = None,
    ) -> None:
        """Emit an execution event to DB and Redis.

        This is the canonical entry point for all execution events.
        """
        settings = get_settings()
        now = datetime.now(timezone.utc).isoformat()
        sequence = await ExecutionEventService._next_sequence(job_id)

        enriched = {
            "job_id": job_id,
            "workspace_id": workspace_id,
            "sequence": sequence,
            "event_type": event_type,
            "payload": {**payload, "trace_id": trace_id},
            "created_at": now,
            "trace_id": trace_id,
        }

        # Persist to DB
        try:
            get_supabase().table("job_events").insert(enriched).execute()
        except Exception as exc:
            logger.warning("DB event persist failed (job=%s, type=%s): %s", job_id, event_type, exc)

        # Publish to Redis for live streaming
        redis = RedisService.get_async_client()
        if redis is not None:
            try:
                encoded = json.dumps(enriched)
                history_key = f"job_events:{job_id}"
                channel = f"job_events:{job_id}:live"
                await redis.rpush(history_key, encoded)
                await redis.ltrim(history_key, -settings.REDIS_JOB_EVENT_MAX_ITEMS, -1)
                if settings.REDIS_JOB_EVENT_TTL_SECONDS > 0:
                    await redis.expire(history_key, settings.REDIS_JOB_EVENT_TTL_SECONDS)
                    await redis.expire(f"job_events:{job_id}:seq", settings.REDIS_JOB_EVENT_TTL_SECONDS)
                await redis.publish(channel, encoded)
            except Exception as exc:
                logger.warning("Redis event publish failed (job=%s, type=%s): %s", job_id, event_type, exc)

    @staticmethod
    async def job_created(job_id: str, *, workspace_id: str | None = None, trace_id: str | None = None, objective: str | None = None) -> None:
        await ExecutionEventService.emit(job_id, EVENT_JOB_CREATED, {"objective": objective}, workspace_id=workspace_id, trace_id=trace_id)

    @staticmethod
    async def planning_started(job_id: str, *, workspace_id: str | None = None, trace_id: str | None = None) -> None:
        await ExecutionEventService.emit(job_id, EVENT_PLANNING_STARTED, {}, workspace_id=workspace_id, trace_id=trace_id)

    @staticmethod
    async def planning_completed(job_id: str, *, workspace_id: str | None = None, trace_id: str | None = None, plan: list[dict[str, Any]] | None = None) -> None:
        await ExecutionEventService.emit(job_id, EVENT_PLANNING_COMPLETED, {"plan": plan or []}, workspace_id=workspace_id, trace_id=trace_id)

    @staticmethod
    async def execution_started(job_id: str, *, workspace_id: str | None = None, trace_id: str | None = None, task_mode: str | None = None) -> None:
        await ExecutionEventService.emit(job_id, EVENT_EXECUTION_STARTED, {"task_mode": task_mode}, workspace_id=workspace_id, trace_id=trace_id)

    @staticmethod
    async def step_started(job_id: str, step: int, tool: str, *, workspace_id: str | None = None, trace_id: str | None = None, args: dict[str, Any] | None = None) -> None:
        await ExecutionEventService.emit(job_id, EVENT_STEP_STARTED, {"step": step, "tool": tool, "args": args or {}}, workspace_id=workspace_id, trace_id=trace_id)

    @staticmethod
    async def step_completed(job_id: str, step: int, tool: str, *, workspace_id: str | None = None, trace_id: str | None = None, success: bool = False, exit_code: int = 0, validation_passed: bool = False) -> None:
        await ExecutionEventService.emit(job_id, EVENT_STEP_COMPLETED, {"step": step, "tool": tool, "success": success, "exit_code": exit_code, "validation_passed": validation_passed}, workspace_id=workspace_id, trace_id=trace_id)

    @staticmethod
    async def validation_started(job_id: str, step: int, *, workspace_id: str | None = None, trace_id: str | None = None, validator: str | None = None) -> None:
        await ExecutionEventService.emit(job_id, EVENT_VALIDATION_STARTED, {"step": step, "validator": validator}, workspace_id=workspace_id, trace_id=trace_id)

    @staticmethod
    async def validation_completed(job_id: str, step: int, *, workspace_id: str | None = None, trace_id: str | None = None, passed: bool = False) -> None:
        await ExecutionEventService.emit(job_id, EVENT_VALIDATION_COMPLETED, {"step": step, "passed": passed}, workspace_id=workspace_id, trace_id=trace_id)

    @staticmethod
    async def retry_started(job_id: str, step: int, attempt: int, *, workspace_id: str | None = None, trace_id: str | None = None) -> None:
        await ExecutionEventService.emit(job_id, EVENT_RETRY_STARTED, {"step": step, "attempt": attempt}, workspace_id=workspace_id, trace_id=trace_id)

    @staticmethod
    async def retry_completed(job_id: str, step: int, attempt: int, *, workspace_id: str | None = None, trace_id: str | None = None, success: bool = False) -> None:
        await ExecutionEventService.emit(job_id, EVENT_RETRY_COMPLETED, {"step": step, "attempt": attempt, "success": success}, workspace_id=workspace_id, trace_id=trace_id)

    @staticmethod
    async def execution_failed(job_id: str, *, workspace_id: str | None = None, trace_id: str | None = None, reason: str | None = None, error: str | None = None) -> None:
        await ExecutionEventService.emit(job_id, EVENT_EXECUTION_FAILED, {"reason": reason, "error": error}, workspace_id=workspace_id, trace_id=trace_id)

    @staticmethod
    async def execution_completed(job_id: str, *, workspace_id: str | None = None, trace_id: str | None = None, success: bool = False, summary: str | None = None) -> None:
        await ExecutionEventService.emit(job_id, EVENT_EXECUTION_COMPLETED, {"success": success, "summary": summary}, workspace_id=workspace_id, trace_id=trace_id)

    @staticmethod
    async def state_transition(
        job_id: str,
        from_status: str,
        to_status: str,
        *,
        workspace_id: str | None = None,
        trace_id: str | None = None,
        step: int | None = None,
        tool: str | None = None,
        reason: str | None = None,
    ) -> None:
        """Record a state transition in both job_events and job_state_transitions.

        This is the canonical path for state transitions.
        """
        now = datetime.now(timezone.utc).isoformat()
        try:
            get_supabase().table("job_state_transitions").insert(
                {
                    "job_id": job_id,
                    "from_status": from_status,
                    "to_status": to_status,
                    "step": step,
                    "tool": tool,
                    "trace_id": trace_id,
                    "reason": reason,
                    "created_at": now,
                }
            ).execute()
        except Exception as exc:
            logger.warning("State transition persist failed (job=%s): %s", job_id, exc)

        await ExecutionEventService.emit(
            job_id,
            "status_update",
            {"from": from_status, "to": to_status, "reason": reason},
            workspace_id=workspace_id,
            trace_id=trace_id,
        )
