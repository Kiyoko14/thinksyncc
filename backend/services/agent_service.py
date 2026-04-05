"""ThinkSync v2 Agent Service.

Orchestrates the real agent loop using OpenAI tool-calling:
  1. User submits a job (objective + server_id)
  2. agent_llm.run_tool_calling_loop() drives an LLM → tool → SSH → LLM cycle
  3. Every tool result is a real SSH operation — nothing is simulated
  4. Job state is persisted to Supabase after every step
  5. WebSocket events are broadcast via per-job asyncio.Queue

Also exposes run_agent() — a lightweight single-turn agent that calls the LLM,
parses one JSON tool call, and dispatches it to POST /api/v1/commands/execute.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

import httpx
from fastapi import HTTPException, status
from openai import AsyncOpenAI
from postgrest.exceptions import APIError

from core.config import get_settings
from core.database import get_supabase
from models.job import JobAccepted, JobCreate, JobResponse, JobStatus
from models.agent import StepResult
from services import agent_llm
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


def _step_to_record(result: StepResult) -> dict[str, Any]:
    """Convert a StepResult to a serialisable dict stored in jobs.steps JSONB."""
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
# Core agent loop  (real execution — no simulation)
# ---------------------------------------------------------------------------


async def _run_agent_loop(
    *,
    job_id: str,
    payload: JobCreate,
    user_id: str,
) -> None:
    """
    Execute the tool-calling agent loop for a queued job.

    Flow:
      1. Fetch the server record (SSH credentials) from Supabase
      2. Delegate to agent_llm.run_tool_calling_loop()
         — LLM issues tool calls → real SSH execution → LLM sees results → repeat
      3. After each tool call: persist step to Supabase + publish WebSocket event
      4. On completion: write final status + summary to Supabase
    """
    settings = get_settings()
    step_timeout = payload.step_timeout_seconds or settings.AGENT_STEP_TIMEOUT

    # --- Transition to RUNNING ---
    _db_update(job_id, {"status": JobStatus.RUNNING.value})
    _publish(job_id, {"type": "status", "status": JobStatus.RUNNING.value})

    # --- Fetch server (real SSH credentials from DB) ---
    try:
        server = ServerService.get_server(server_id=payload.server_id, user_id=user_id)
    except HTTPException as exc:
        _db_update(job_id, {"status": JobStatus.FAILED.value, "summary": exc.detail})
        _publish(job_id, {"type": "error", "detail": exc.detail})
        return

    # Persist current steps list progressively
    accumulated_steps: list[dict[str, Any]] = []

    # Callbacks invoked by the LLM loop so we can stream events in real time
    async def on_step_start(step_num: int, tool_name: str, args: dict[str, Any]) -> None:
        _db_update(job_id, {"status": JobStatus.WAITING_FOR_LLM.value})
        _publish(job_id, {
            "type": "step_start",
            "step": step_num,
            "tool": tool_name,
            "args": args,
        })

    async def on_step_result(result: StepResult) -> None:
        record = _step_to_record(result)
        accumulated_steps.append(record)
        _db_update(job_id, {
            "status": JobStatus.RUNNING.value,
            "steps": accumulated_steps,
        })
        _publish(job_id, {
            "type": "step_result",
            "step": result.step,
            "tool": result.tool.value,
            "success": result.success,
            "exit_code": result.exit_code,
            "stdout": result.stdout[:500],
            "stderr": result.stderr[:200],
        })

    # --- Run the real tool-calling loop ---
    try:
        loop_result = await agent_llm.run_tool_calling_loop(
            objective=payload.objective,
            server=server,
            allow_write=payload.allow_write,
            max_steps=payload.max_steps,
            step_timeout=step_timeout,
            on_step_start=on_step_start,
            on_step_result=on_step_result,
        )
    except HTTPException as exc:
        _db_update(job_id, {"status": JobStatus.FAILED.value, "summary": exc.detail})
        _publish(job_id, {"type": "error", "detail": exc.detail})
        return
    except Exception as exc:
        logger.exception("Unhandled error in tool-calling loop (job=%s): %s", job_id, exc)
        detail = f"Agent loop failed: {exc}"
        _db_update(job_id, {"status": JobStatus.FAILED.value, "summary": detail})
        _publish(job_id, {"type": "error", "detail": detail})
        return

    final_status = JobStatus.COMPLETED if loop_result.success else JobStatus.FAILED
    _db_update(job_id, {
        "status": final_status.value,
        "summary": loop_result.summary,
        "steps": accumulated_steps,
    })
    _publish(job_id, {
        "type": "completed",
        "success": loop_result.success,
        "summary": loop_result.summary,
        "steps_taken": loop_result.steps_taken,
    })


# ---------------------------------------------------------------------------
# Public service API
# ---------------------------------------------------------------------------


class AgentService:
    """Service layer for ThinkSync v2 job management."""

    @staticmethod
    def create_job(user_id: str, payload: JobCreate) -> JobAccepted:
        """Insert a new job row in Supabase and register a WebSocket event queue."""
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
        """Fetch a single job owned by the authenticated user."""
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
        """Return the in-memory WebSocket event queue for a running job."""
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
        """Background coroutine: acquire concurrency semaphore, then run the agent loop."""
        sem = _get_semaphore()
        async with sem:
            try:
                await _run_agent_loop(job_id=job_id, payload=payload, user_id=user_id)
            except Exception as exc:
                logger.exception("Unhandled error in agent loop (job=%s): %s", job_id, exc)
                _db_update(job_id, {"status": JobStatus.FAILED.value, "summary": str(exc)})
                _publish(job_id, {"type": "error", "detail": str(exc)})


# ---------------------------------------------------------------------------
# Lightweight single-turn execution agent
# ---------------------------------------------------------------------------

_RUN_AGENT_SYSTEM = """\
You are Forge, a production-grade DevOps execution agent running inside ThinkSync.

══════════════════════════════════════════
CRITICAL RULES — NEVER VIOLATE
══════════════════════════════════════════
1. Respond with ONLY a single JSON object. No explanation, no markdown, no prose.
2. You MUST always emit a tool call. Never simulate or assume results.
3. NEVER reference local paths such as /home/root/workspaces, /tmp/local, or similar.
4. All file paths must be paths on the REMOTE server.
5. If the objective is unclear, emit the safest possible diagnostic command.

══════════════════════════════════════════
AVAILABLE TOOL
══════════════════════════════════════════
run_command — execute a shell command on the remote server via SSH.

══════════════════════════════════════════
REQUIRED OUTPUT FORMAT  (strict JSON — no markdown, no extra keys)
══════════════════════════════════════════
{
  "tool": "run_command",
  "input": {
    "command": "<shell command to execute on the remote server>"
  }
}

══════════════════════════════════════════
SAFETY — ABSOLUTELY FORBIDDEN
══════════════════════════════════════════
Never include commands that:
  ✗ Delete data:            rm -rf, shred, wipefs
  ✗ Destroy storage:        mkfs, dd if=, > /dev/sd*
  ✗ Shut down the system:   shutdown, reboot, poweroff, halt, init 0/6
  ✗ Modify credentials:     passwd, chpasswd, usermod
  ✗ Pipe to a shell:        curl ... | bash, wget ... | sh, ... | python -c
  ✗ Escalate dangerously:   chmod 777 on system directories
"""


async def run_agent(
    server_id: str,
    objective: str,
    token: str,
) -> dict[str, Any]:
    """Lightweight single-turn execution agent.

    Flow:
      1. Send the objective to the LLM with a strict JSON-only system prompt.
      2. Parse the LLM response as JSON (expects {"tool": ..., "input": {...}}).
      3. For tool == "run_command", call POST /api/v1/commands/execute via httpx,
         passing the Bearer token so the existing auth/SSH chain handles execution.
      4. Return the real command result or a structured error dict.

    Args:
        server_id: UUID of the server to run the command on.
        objective:  Natural-language instruction from the user.
        token:      JWT Bearer token of the authenticated user.

    Returns:
        A dict with keys ``tool``, ``command``, and ``result`` on success,
        or a dict with key ``error`` (and optional context) on failure.
    """
    settings = get_settings()

    if not settings.OPENAI_API_KEY:
        logger.error("[run_agent] OPENAI_API_KEY is not configured")
        return {"error": "OPENAI_API_KEY is not configured on the server"}

    client = AsyncOpenAI(api_key=settings.OPENAI_API_KEY)
    user_message = f"Objective: {objective}\nServer ID: {server_id}"

    # ── 1. Call LLM ──────────────────────────────────────────────────────────
    logger.info("[run_agent] Calling LLM | objective=%r | server_id=%s", objective, server_id)
    try:
        response = await client.chat.completions.create(
            model=settings.OPENAI_MODEL,
            messages=[
                {"role": "system", "content": _RUN_AGENT_SYSTEM},
                {"role": "user", "content": user_message},
            ],
            response_format={"type": "json_object"},
            temperature=0.1,
        )
    except Exception as exc:
        logger.error("[run_agent] LLM call failed: %s", exc)
        return {"error": f"LLM request failed: {exc}"}

    raw = (response.choices[0].message.content or "").strip()
    logger.info("[run_agent] LLM raw response: %s", raw)

    # ── 2. Parse JSON ────────────────────────────────────────────────────────
    try:
        parsed: dict[str, Any] = json.loads(raw)
    except json.JSONDecodeError as exc:
        logger.error("[run_agent] LLM returned invalid JSON: %s", exc)
        return {"error": f"LLM returned invalid JSON: {exc}", "raw": raw}

    logger.info("[run_agent] Parsed JSON: %s", parsed)

    # ── 3. Validate tool and command ─────────────────────────────────────────
    tool = parsed.get("tool")
    if not tool:
        logger.error("[run_agent] LLM response missing 'tool' field: %s", parsed)
        return {"error": "LLM response missing 'tool' field", "parsed": parsed}

    if tool != "run_command":
        logger.error("[run_agent] Unknown tool %r in LLM response", tool)
        return {
            "error": f"Unknown tool: {tool!r}. Only 'run_command' is supported.",
            "parsed": parsed,
        }

    tool_input: dict[str, Any] = parsed.get("input") or {}
    command: str = (tool_input.get("command") or "").strip()
    if not command:
        logger.error("[run_agent] LLM tool call missing 'command': %s", parsed)
        return {"error": "LLM tool call missing 'command' field", "parsed": parsed}

    # ── 4. Call POST /api/v1/commands/execute via httpx ──────────────────────
    api_host = "127.0.0.1" if settings.API_HOST in ("0.0.0.0", "") else settings.API_HOST
    execute_url = f"http://{api_host}:{settings.API_PORT}/api/v1/commands/execute"
    request_payload = {"server_id": server_id, "command": command}

    logger.info(
        "[run_agent] Executing tool=run_command | command=%r | url=%s",
        command,
        execute_url,
    )
    try:
        async with httpx.AsyncClient(timeout=settings.SSH_COMMAND_TIMEOUT + 10) as http:
            resp = await http.post(
                execute_url,
                json=request_payload,
                headers={"Authorization": f"Bearer {token}"},
            )
            resp.raise_for_status()
            result: dict[str, Any] = resp.json()
    except httpx.HTTPStatusError as exc:
        logger.error(
            "[run_agent] /commands/execute returned HTTP %s: %s",
            exc.response.status_code,
            exc.response.text,
        )
        return {
            "error": f"Command execution API error: HTTP {exc.response.status_code}",
            "detail": exc.response.text,
        }
    except Exception as exc:
        logger.error("[run_agent] /commands/execute request failed: %s", exc)
        return {"error": f"Command execution request failed: {exc}"}

    logger.info(
        "[run_agent] Tool result | exit_code=%s | stdout_preview=%r",
        result.get("exit_code"),
        str(result.get("stdout", ""))[:200],
    )

    return {
        "tool": tool,
        "command": command,
        "result": result,
    }
