"""Unified ThinkSync agent runtime.

Single source of truth:
- all execution flows through agent_llm.run_tool_calling_loop()
- jobs persist to Supabase
- chat memory persists to Redis + DB
- job event streaming persists to Redis and replays over WebSocket
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from fastapi import HTTPException, status
from postgrest.exceptions import APIError

from core.config import get_settings
from core.database import get_supabase
from models.agent import AgentDecision, AgentStep, AgentTier, StepResult
from models.job import JobAccepted, JobCreate, JobResponse, JobStatus
from services import agent_llm
from services.chat_service import ChatService
from services.redis_service import RedisService
from services.server_service import ServerService
from services.workspace_service import WorkspaceService

logger = logging.getLogger(__name__)

_TABLE = "jobs"
_local_subscribers: dict[str, set[asyncio.Queue[dict[str, Any]]]] = {}
_local_event_history: dict[str, list[dict[str, Any]]] = {}
_local_event_seq: dict[str, int] = {}
_semaphore: asyncio.Semaphore | None = None


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _get_semaphore() -> asyncio.Semaphore:
    global _semaphore
    if _semaphore is None:
        _semaphore = asyncio.Semaphore(get_settings().AGENT_MAX_CONCURRENCY)
    return _semaphore


def _db_update(job_id: str, patch: dict[str, Any]) -> None:
    try:
        patch["updated_at"] = _now_iso()
        get_supabase().table(_TABLE).update(patch).eq("id", job_id).execute()
    except APIError as exc:
        logger.warning("jobs UPDATE failed (job=%s): %s", job_id, exc)


def _step_to_record(result: StepResult) -> dict[str, Any]:
    return {
        "step": result.step,
        "tool": result.tool.value,
        "args": result.args,
        "stdout": result.stdout[:4000],
        "stderr": result.stderr[:2000],
        "exit_code": result.exit_code,
        "duration_ms": result.duration_ms,
        "success": result.success,
        "executed_at": result.executed_at.isoformat(),
    }


def _decision_to_record(decision: AgentDecision) -> dict[str, Any]:
    return {
        "action": decision.action.value,
        "reason": decision.reason,
        "summary_so_far": decision.summary_so_far,
        "modified_step": decision.modified_step.model_dump(mode="json") if decision.modified_step else None,
    }


def _row_to_response(row: dict[str, Any]) -> JobResponse:
    return JobResponse(
        id=row["id"],
        user_id=row["user_id"],
        workspace_id=row.get("workspace_id"),
        server_id=row["server_id"],
        objective=row["objective"],
        status=JobStatus(row["status"]),
        allow_write=bool(row.get("allow_write", False)),
        dry_run=bool(row.get("dry_run", False)),
        task_mode=row.get("task_mode") or "complex",
        plan=row.get("plan") or [],
        steps=row.get("steps") or [],
        decisions=row.get("decisions") or [],
        summary=row.get("summary"),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


async def _next_event_sequence(job_id: str) -> int:
    redis = RedisService.get_async_client()
    if redis is not None:
        try:
            return int(await redis.incr(f"job_events:{job_id}:seq"))
        except Exception as exc:
            logger.warning("Redis INCR failed for job=%s: %s", job_id, exc)
    next_value = _local_event_seq.get(job_id, 0) + 1
    _local_event_seq[job_id] = next_value
    return next_value


async def _publish(job_id: str, event: dict[str, Any]) -> None:
    settings = get_settings()
    enriched = {
        "timestamp": event.get("timestamp") or _now_iso(),
        "sequence": event.get("sequence") or await _next_event_sequence(job_id),
        **event,
    }
    encoded = json.dumps(enriched)
    redis = RedisService.get_async_client()
    if redis is not None:
        history_key = f"job_events:{job_id}"
        channel = f"job_events:{job_id}:live"
        try:
            await redis.rpush(history_key, encoded)
            await redis.ltrim(history_key, -settings.REDIS_JOB_EVENT_MAX_ITEMS, -1)
            if settings.REDIS_JOB_EVENT_TTL_SECONDS > 0:
                await redis.expire(history_key, settings.REDIS_JOB_EVENT_TTL_SECONDS)
                await redis.expire(f"job_events:{job_id}:seq", settings.REDIS_JOB_EVENT_TTL_SECONDS)
            await redis.publish(channel, encoded)
        except Exception as exc:
            logger.warning("Redis publish failed for job=%s: %s", job_id, exc)

    history = _local_event_history.setdefault(job_id, [])
    history.append(enriched)
    history[:] = history[-settings.REDIS_JOB_EVENT_MAX_ITEMS:]
    for queue in list(_local_subscribers.get(job_id, set())):
        try:
            queue.put_nowait(enriched)
        except asyncio.QueueFull:
            pass


async def run_agent_pipeline(*, job_id: str, payload: JobCreate, user_id: str) -> None:
    settings = get_settings()
    step_timeout = payload.step_timeout_seconds or settings.AGENT_STEP_TIMEOUT

    _db_update(job_id, {"status": JobStatus.RUNNING.value})
    await _publish(job_id, {"type": "status_update", "status": JobStatus.RUNNING.value, "step": 0, "tool": None})

    try:
        server = ServerService.get_server(server_id=payload.server_id, user_id=user_id)
    except HTTPException as exc:
        _db_update(job_id, {"status": JobStatus.FAILED.value, "summary": exc.detail})
        await _publish(job_id, {"type": "completed", "success": False, "summary": str(exc.detail), "step": 0, "tool": None})
        return

    if not payload.workspace_id:
        detail = {"code": "WORKSPACE_REQUIRED"}
        _db_update(job_id, {"status": JobStatus.FAILED.value, "summary": json.dumps(detail)})
        await _publish(job_id, {"type": "completed", "success": False, "summary": json.dumps(detail), "step": 0, "tool": None})
        return

    try:
        workspace = WorkspaceService.get_workspace_by_id(id=payload.workspace_id, user_id=user_id)
    except HTTPException as exc:
        _db_update(job_id, {"status": JobStatus.FAILED.value, "summary": exc.detail})
        await _publish(job_id, {"type": "completed", "success": False, "summary": str(exc.detail), "step": 0, "tool": None})
        return

    if workspace.get("server_id") != payload.server_id:
        detail = "workspace_id does not belong to the provided server_id"
        _db_update(job_id, {"status": JobStatus.FAILED.value, "summary": detail})
        await _publish(job_id, {"type": "completed", "success": False, "summary": detail, "step": 0, "tool": None})
        return

    workspace_path = str(workspace.get("path") or "").strip()
    if not workspace_path:
        detail = "Workspace path is missing"
        _db_update(job_id, {"status": JobStatus.FAILED.value, "summary": detail})
        await _publish(job_id, {"type": "completed", "success": False, "summary": detail, "step": 0, "tool": None})
        return

    accumulated_steps: list[dict[str, Any]] = []
    accumulated_decisions: list[dict[str, Any]] = []
    conversation_history: list[dict[str, str]] = []
    if payload.workspace_id:
        try:
            # Save user objective if it's a new interaction
            ChatService.save_workspace_message(
                workspace_id=payload.workspace_id,
                user_id=user_id,
                role="user",
                content=payload.objective,
            )
            conversation_history = ChatService.get_recent_context_messages(
                workspace_id=payload.workspace_id,
                user_id=user_id,
                limit=20,
                current_input=payload.objective,
            )
        except HTTPException as exc:
            _db_update(job_id, {"status": JobStatus.FAILED.value, "summary": exc.detail})
            await _publish(job_id, {"type": "completed", "success": False, "summary": str(exc.detail), "step": 0, "tool": None})
            return

    async def on_plan(plan: list[AgentStep], task_mode: str) -> None:
        plan_records = [step.model_dump(mode="json") for step in plan]
        _db_update(job_id, {"plan": plan_records, "task_mode": task_mode})
        await _publish(
            job_id,
            {
                "type": "status_update",
                "status": JobStatus.WAITING_FOR_LLM.value,
                "step": 0,
                "tool": "planner",
                "task_mode": task_mode,
                "plan": plan_records,
            },
        )

    async def on_step_start(step_num: int, tool_name: str, args: dict[str, Any]) -> None:
        _db_update(job_id, {"status": JobStatus.RUNNING.value})
        await _publish(
            job_id,
            {
                "type": "step_start",
                "status": JobStatus.RUNNING.value,
                "step": step_num,
                "tool": tool_name,
                "args": args,
            },
        )

    async def on_log_chunk(step_num: int, tool_name: str, stream: str, chunk: str) -> None:
        await _publish(
            job_id,
            {
                "type": "log_chunk",
                "status": JobStatus.RUNNING.value,
                "step": step_num,
                "tool": tool_name,
                "stream": stream,
                "data": chunk,
                "stdout_preview": chunk[:300] if stream == "stdout" else "",
                "stderr_preview": chunk[:300] if stream == "stderr" else "",
            },
        )

    async def on_step_result(result: StepResult) -> None:
        record = _step_to_record(result)
        accumulated_steps.append(record)
        _db_update(job_id, {"steps": accumulated_steps, "status": JobStatus.RUNNING.value})
        await _publish(
            job_id,
            {
                "type": "step_result",
                "status": JobStatus.RUNNING.value,
                "step": result.step,
                "tool": result.tool.value,
                "success": result.success,
                "exit_code": result.exit_code,
                "stdout_preview": result.stdout[:500],
                "stderr_preview": result.stderr[:300],
            },
        )

    async def on_decision(decision: AgentDecision) -> None:
        record = _decision_to_record(decision)
        accumulated_decisions.append(record)
        _db_update(job_id, {"decisions": accumulated_decisions, "status": JobStatus.WAITING_FOR_LLM.value})
        await _publish(
            job_id,
            {
                "type": "status_update",
                "status": JobStatus.WAITING_FOR_LLM.value,
                "step": len(accumulated_steps),
                "tool": "evaluator",
                "decision": record,
            },
        )

    try:
        loop_result = await agent_llm.run_tool_calling_loop(
            objective=payload.objective,
            server=server,
            workspace_path=workspace_path,
            allow_write=payload.allow_write,
            max_steps=payload.max_steps,
            step_timeout=step_timeout,
            conversation_history=conversation_history,
            on_step_start=on_step_start,
            on_step_result=on_step_result,
            on_log_chunk=on_log_chunk,
            on_plan=on_plan,
            on_decision=on_decision,
        )
    except HTTPException as exc:
        detail = exc.detail if isinstance(exc.detail, str) else json.dumps(exc.detail)
        _db_update(job_id, {"status": JobStatus.FAILED.value, "summary": detail})
        if payload.workspace_id:
            try:
                ChatService.save_workspace_message(
                    workspace_id=payload.workspace_id,
                    user_id=user_id,
                    role="assistant",
                    content=detail,
                )
            except Exception:
                logger.warning("Failed to persist assistant error message for job=%s", job_id)
        await _publish(job_id, {"type": "completed", "success": False, "summary": detail, "step": 0, "tool": None})
        return
    except Exception as exc:
        logger.exception("Unhandled error in agent loop (job=%s): %s", job_id, exc)
        detail = f"Agent loop failed: {exc}"
        _db_update(job_id, {"status": JobStatus.FAILED.value, "summary": detail})
        await _publish(job_id, {"type": "completed", "success": False, "summary": detail, "step": 0, "tool": None})
        return

    final_status = JobStatus.COMPLETED if loop_result.success else JobStatus.FAILED
    _db_update(
        job_id,
        {
            "status": final_status.value,
            "summary": loop_result.summary,
            "task_mode": loop_result.task_mode,
            "plan": [step.model_dump(mode="json") for step in loop_result.plan],
            "steps": accumulated_steps or [_step_to_record(step) for step in loop_result.steps],
            "decisions": accumulated_decisions or [_decision_to_record(decision) for decision in loop_result.decisions],
        },
    )
    if payload.workspace_id:
        try:
            ChatService.save_workspace_message(
                workspace_id=payload.workspace_id,
                user_id=user_id,
                role="assistant",
                content=loop_result.summary,
            )
        except Exception:
            logger.warning("Failed to persist assistant summary for job=%s", job_id)

    await _publish(
        job_id,
        {
            "type": "completed",
            "status": final_status.value,
            "success": loop_result.success,
            "summary": loop_result.summary,
            "step": loop_result.steps_taken,
            "tool": None,
        },
    )


async def _run_agent_loop(*, job_id: str, payload: JobCreate, user_id: str) -> None:
    # This matches the old signature but now calls the new pipeline
    await run_agent_pipeline(job_id=job_id, payload=payload, user_id=user_id)


class AgentService:
    @staticmethod
    def submit_job(user_id: str, payload: JobCreate) -> JobAccepted:
        if payload.dry_run:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="dry_run is disabled for the production execution pipeline.",
            )
        if not payload.workspace_id:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail={"code": "WORKSPACE_REQUIRED"})

        workspace = WorkspaceService.get_workspace_by_id(id=payload.workspace_id, user_id=user_id)
        if workspace["server_id"] != payload.server_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="workspace_id does not belong to the provided server_id",
            )
        ChatService.create_chat(workspace_id=payload.workspace_id, user_id=user_id)
        ChatService.save_workspace_message(
            workspace_id=payload.workspace_id,
            user_id=user_id,
            role="user",
            content=payload.objective,
        )
        return AgentService.create_job(user_id=user_id, payload=payload)

    @staticmethod
    def create_job(user_id: str, payload: JobCreate) -> JobAccepted:
        job_id = str(uuid4())
        now = _now_iso()
        record: dict[str, Any] = {
            "id": job_id,
            "user_id": user_id,
            "workspace_id": payload.workspace_id,
            "server_id": payload.server_id,
            "objective": payload.objective,
            "status": JobStatus.QUEUED.value,
            "allow_write": payload.allow_write,
            "dry_run": payload.dry_run,
            "task_mode": "complex",
            "plan": [],
            "steps": [],
            "decisions": [],
            "summary": None,
            "created_at": now,
            "updated_at": now,
        }

        try:
            result = get_supabase().table(_TABLE).insert(record).execute()
        except APIError as exc:
            logger.error("Failed to insert job row: %s", exc)
            raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to create job")

        if not result or not result.data:
            raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to create job")

        _local_subscribers[job_id] = set()
        _local_event_history[job_id] = []
        _local_event_seq[job_id] = 0
        return JobAccepted(id=job_id)

    @staticmethod
    def get_job(job_id: str, user_id: str) -> JobResponse:
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
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")

        if not result or not result.data:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")
        return _row_to_response(result.data)

    @staticmethod
    def list_jobs(user_id: str, workspace_id: str | None = None) -> list[JobResponse]:
        if not workspace_id:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail={"code": "WORKSPACE_REQUIRED"})

        WorkspaceService.get_workspace_by_id(id=workspace_id, user_id=user_id)
        query = get_supabase().table(_TABLE).select("*").eq("user_id", user_id).eq("workspace_id", workspace_id)
        result = query.order("created_at", desc=True).execute()
        return [_row_to_response(row) for row in result.data or []]

    @staticmethod
    async def get_event_history(job_id: str) -> list[dict[str, Any]]:
        redis = RedisService.get_async_client()
        if redis is not None:
            try:
                raw_items = await redis.lrange(f"job_events:{job_id}", 0, -1)
                return [json.loads(item) for item in raw_items]
            except Exception as exc:
                logger.warning("Failed to fetch Redis event history for job=%s: %s", job_id, exc)
        return list(_local_event_history.get(job_id, []))

    @staticmethod
    def subscribe(job_id: str) -> asyncio.Queue[dict[str, Any]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=500)
        _local_subscribers.setdefault(job_id, set()).add(queue)
        return queue

    @staticmethod
    def unsubscribe(job_id: str, queue: asyncio.Queue[dict[str, Any]]) -> None:
        subscribers = _local_subscribers.get(job_id)
        if subscribers is None:
            return
        subscribers.discard(queue)

    @staticmethod
    async def run_job(job_id: str, payload: JobCreate, user_id: str) -> None:
        async with _get_semaphore():
            await _run_agent_loop(job_id=job_id, payload=payload, user_id=user_id)


def to_forge_v2_response(job: JobResponse) -> dict[str, Any]:
    status_value = job.status.value if isinstance(job.status, JobStatus) else str(job.status)
    run: dict[str, Any] | None = {
        "agent": AgentTier.FORGE_V2.value,
        "job_id": job.id,
        "objective": job.objective,
        "dry_run": job.dry_run,
        "plan": job.plan or [],
        "results": job.steps or [],
        "decisions": job.decisions or [],
        "summary": job.summary or "",
        "success": status_value == JobStatus.COMPLETED.value,
    }
    error: str | None = None

    if status_value == JobStatus.QUEUED.value:
        run = None
    elif status_value == JobStatus.FAILED.value:
        error = job.summary or "Job failed"

    return {
        "job_id": job.id,
        "status": status_value,
        "run": run,
        "error": error,
    }
