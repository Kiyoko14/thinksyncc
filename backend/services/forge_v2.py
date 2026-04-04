"""Forge v2 — self-correcting AI agent with LLM-based dynamic planning.

Architecture:
- Tool abstraction layer: run_command, check_disk, check_memory,
  restart_service, read_logs, deploy_app
- Agent loop: generate plan → execute → evaluate → retry/continue/abort
- Async job management with status tracking
- Full audit logging to Supabase (agent_runs table)
- WebSocket-friendly event broadcasting via asyncio.Queue
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from fastapi import HTTPException, status
from postgrest.exceptions import APIError

from core.config import get_settings
from core.database import get_supabase
from models.agent import (
    AgentDecision,
    AgentJobStatus,
    AgentPlan,
    AgentStep,
    AgentTier,
    DecisionAction,
    ForgeV2JobResponse,
    ForgeV2PlanResponse,
    ForgeV2RunRequest,
    ForgeV2RunResponse,
    StepResult,
    ToolName,
)
from services import agent_llm
from services.server_service import ServerService
from services.ssh_service import SSHService

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Safety constants
# ---------------------------------------------------------------------------

_BLOCKED_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"\brm\s+-rf\b", flags=re.IGNORECASE),
    re.compile(r"\bmkfs\b", flags=re.IGNORECASE),
    re.compile(r"\bdd\s+if=", flags=re.IGNORECASE),
    re.compile(r"\bshutdown\b", flags=re.IGNORECASE),
    re.compile(r"\breboot\b", flags=re.IGNORECASE),
    re.compile(r"\bpasswd\b", flags=re.IGNORECASE),
    re.compile(r"\bchmod\s+777\b", flags=re.IGNORECASE),
    re.compile(r">\s*/dev/sd", flags=re.IGNORECASE),
]

_READ_ONLY_COMMAND_PREFIXES: tuple[str, ...] = (
    "uname",
    "uptime",
    "whoami",
    "id",
    "pwd",
    "ls",
    "df",
    "free",
    "cat",
    "head",
    "tail",
    "ps",
    "ss",
    "netstat",
    "docker ps",
    "docker images",
    "docker stats",
    "systemctl status",
    "journalctl",
    "echo",
    "hostname",
    "date",
    "top -bn1",
    "vmstat",
    "iostat",
    "lscpu",
    "lsblk",
)

_WRITE_TOOLS: tuple[ToolName, ...] = (ToolName.RESTART_SERVICE, ToolName.DEPLOY_APP)

# ---------------------------------------------------------------------------
# In-memory job store  { job_id: { status, run, error, events_queue } }
# ---------------------------------------------------------------------------

_jobs: dict[str, dict[str, Any]] = {}


# ---------------------------------------------------------------------------
# Safety helpers
# ---------------------------------------------------------------------------


def _is_dangerous(command: str) -> bool:
    for pattern in _BLOCKED_PATTERNS:
        if pattern.search(command):
            return True
    return False


def _is_allowed_prefix(command: str, allow_write: bool) -> bool:
    settings = get_settings()
    custom = [p.strip().lower() for p in settings.AGENT_ALLOWED_COMMAND_PREFIXES.split(",") if p.strip()]
    allowed_prefixes: tuple[str, ...] = tuple(custom) if custom else _READ_ONLY_COMMAND_PREFIXES

    if allow_write:
        return True

    lowered = command.strip().lower()
    return any(lowered.startswith(prefix) for prefix in allowed_prefixes)


def _validate_command(command: str, allow_write: bool) -> None:
    if _is_dangerous(command):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Rejected dangerous command: {command!r}",
        )
    if not _is_allowed_prefix(command, allow_write=allow_write):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Command not in allowlist: {command!r}",
        )


def _validate_service_name(name: str) -> None:
    if not re.match(r'^[a-zA-Z0-9_.\\-]+$', name):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid service name: {name!r}",
        )


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------


async def _tool_run_command(
    args: dict[str, Any],
    server: dict[str, Any],
    allow_write: bool,
    timeout: int,
) -> tuple[str, str, int]:
    command: str = args.get("command", "").strip()
    if not command:
        return "", "run_command: 'command' arg is required", 1
    _validate_command(command, allow_write=allow_write)
    resp = await SSHService.execute(server=server, command=command, command_timeout=timeout)
    stdout = resp.output if resp.exit_code == 0 else ""
    stderr = resp.output if resp.exit_code != 0 else ""
    return stdout, stderr, resp.exit_code


async def _tool_check_disk(
    args: dict[str, Any],
    server: dict[str, Any],
    allow_write: bool,
    timeout: int,
) -> tuple[str, str, int]:
    resp = await SSHService.execute(server=server, command="df -h", command_timeout=timeout)
    return resp.output, "", resp.exit_code


async def _tool_check_memory(
    args: dict[str, Any],
    server: dict[str, Any],
    allow_write: bool,
    timeout: int,
) -> tuple[str, str, int]:
    resp = await SSHService.execute(server=server, command="free -m", command_timeout=timeout)
    return resp.output, "", resp.exit_code


async def _tool_restart_service(
    args: dict[str, Any],
    server: dict[str, Any],
    allow_write: bool,
    timeout: int,
) -> tuple[str, str, int]:
    if not allow_write:
        return "", "restart_service requires allow_write=true", 1
    service_name: str = args.get("service_name", "").strip()
    _validate_service_name(service_name)
    command = f"systemctl restart {service_name}"
    resp = await SSHService.execute(server=server, command=command, command_timeout=timeout)
    stdout = resp.output if resp.exit_code == 0 else ""
    stderr = resp.output if resp.exit_code != 0 else ""
    return stdout, stderr, resp.exit_code


async def _tool_read_logs(
    args: dict[str, Any],
    server: dict[str, Any],
    allow_write: bool,
    timeout: int,
) -> tuple[str, str, int]:
    service_name: str = args.get("service_name", "").strip()
    lines: int = int(args.get("lines", 100))
    lines = max(1, min(lines, 1000))

    if service_name.startswith("/"):
        # Reading a log file path
        if _is_dangerous(f"tail -n {lines} {service_name}"):
            return "", f"Rejected dangerous log path: {service_name!r}", 1
        command = f"tail -n {lines} {service_name}"
    else:
        _validate_service_name(service_name)
        command = f"journalctl -u {service_name} -n {lines} --no-pager"

    resp = await SSHService.execute(server=server, command=command, command_timeout=timeout)
    stdout = resp.output if resp.exit_code == 0 else ""
    stderr = resp.output if resp.exit_code != 0 else ""
    return stdout, stderr, resp.exit_code


async def _tool_deploy_app(
    args: dict[str, Any],
    server: dict[str, Any],
    allow_write: bool,
    timeout: int,
) -> tuple[str, str, int]:
    if not allow_write:
        return "", "deploy_app requires allow_write=true", 1
    app_name: str = args.get("app_name", "").strip()
    deploy_command: str = args.get("deploy_command", "").strip()

    if not app_name or not deploy_command:
        return "", "deploy_app requires 'app_name' and 'deploy_command'", 1

    _validate_command(deploy_command, allow_write=True)

    resp = await SSHService.execute(server=server, command=deploy_command, command_timeout=timeout)
    stdout = resp.output if resp.exit_code == 0 else ""
    stderr = resp.output if resp.exit_code != 0 else ""
    return stdout, stderr, resp.exit_code


_TOOL_DISPATCH: dict[ToolName, Any] = {
    ToolName.RUN_COMMAND: _tool_run_command,
    ToolName.CHECK_DISK: _tool_check_disk,
    ToolName.CHECK_MEMORY: _tool_check_memory,
    ToolName.RESTART_SERVICE: _tool_restart_service,
    ToolName.READ_LOGS: _tool_read_logs,
    ToolName.DEPLOY_APP: _tool_deploy_app,
}


async def _execute_tool(
    step: AgentStep,
    server: dict[str, Any],
    allow_write: bool,
    timeout: int,
) -> StepResult:
    fn = _TOOL_DISPATCH.get(step.tool)
    if fn is None:
        return StepResult(
            step=step.step,
            tool=step.tool,
            args=step.args,
            stderr=f"Unknown tool: {step.tool}",
            exit_code=1,
            duration_ms=0,
            executed_at=datetime.now(timezone.utc),
            success=False,
        )

    start_ms = time.monotonic()
    try:
        stdout, stderr, exit_code = await asyncio.wait_for(
            fn(step.args, server, allow_write, timeout),
            timeout=timeout + 5,
        )
    except asyncio.TimeoutError:
        stdout, stderr, exit_code = "", "Tool execution timed out", 124
    except HTTPException as exc:
        stdout, stderr, exit_code = "", str(exc.detail), 1
    except Exception as exc:
        logger.exception("Unexpected error in tool %s: %s", step.tool, exc)
        stdout, stderr, exit_code = "", f"Internal tool error: {exc}", 1

    duration_ms = int((time.monotonic() - start_ms) * 1000)
    return StepResult(
        step=step.step,
        tool=step.tool,
        args=step.args,
        stdout=stdout,
        stderr=stderr,
        exit_code=exit_code,
        duration_ms=duration_ms,
        executed_at=datetime.now(timezone.utc),
        success=(exit_code == 0),
    )


# ---------------------------------------------------------------------------
# Audit logging
# ---------------------------------------------------------------------------


def _audit_step(
    *,
    job_id: str,
    user_id: str,
    server_id: str,
    objective: str,
    result: StepResult,
    decision: AgentDecision,
) -> None:
    settings = get_settings()
    if not settings.AGENT_AUDIT_LOGGING_ENABLED:
        return

    supabase = get_supabase()
    record: dict[str, Any] = {
        "user_id": user_id,
        "server_id": server_id,
        "objective": objective,
        "agent": AgentTier.FORGE_V2.value,
        "job_id": job_id,
        "step": result.step,
        "tool": result.tool.value,
        "args": result.args,
        "stdout": result.stdout[:4000],
        "stderr": result.stderr[:2000],
        "exit_code": result.exit_code,
        "duration_ms": result.duration_ms,
        "decision": decision.action.value,
        "decision_reason": decision.reason[:1000],
        "success": result.success,
        "created_at": result.executed_at.isoformat(),
    }

    try:
        supabase.table(settings.AGENT_AUDIT_TABLE).insert(record).execute()
    except APIError as exc:
        logger.warning("Audit log insert failed: %s", exc)


def _audit_run_complete(
    *,
    job_id: str,
    user_id: str,
    server_id: str,
    objective: str,
    summary: str,
    success: bool,
    dry_run: bool,
) -> None:
    settings = get_settings()
    if not settings.AGENT_AUDIT_LOGGING_ENABLED:
        return

    supabase = get_supabase()
    record: dict[str, Any] = {
        "user_id": user_id,
        "server_id": server_id,
        "objective": objective,
        "agent": AgentTier.FORGE_V2.value,
        "job_id": job_id,
        "step": 0,
        "tool": "run_complete",
        "args": {},
        "stdout": summary[:4000],
        "stderr": "",
        "exit_code": 0 if success else 1,
        "duration_ms": 0,
        "decision": "completed" if success else "failed",
        "decision_reason": f"dry_run={dry_run}",
        "success": success,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }

    try:
        supabase.table(settings.AGENT_AUDIT_TABLE).insert(record).execute()
    except APIError as exc:
        logger.warning("Audit log (run_complete) insert failed: %s", exc)


# ---------------------------------------------------------------------------
# Event broadcasting
# ---------------------------------------------------------------------------


def _publish_event(job_id: str, event: dict[str, Any]) -> None:
    job = _jobs.get(job_id)
    if job is None:
        return
    q: asyncio.Queue[dict[str, Any]] | None = job.get("events_queue")
    if q is not None:
        try:
            q.put_nowait(event)
        except asyncio.QueueFull:
            pass


# ---------------------------------------------------------------------------
# Core agent loop
# ---------------------------------------------------------------------------


async def _run_agent_loop(
    *,
    job_id: str,
    payload: ForgeV2RunRequest,
    server: dict[str, Any],
    user_id: str,
) -> ForgeV2RunResponse:
    settings = get_settings()
    max_retries = payload.max_retries if payload.max_retries is not None else settings.AGENT_MAX_RETRIES
    step_timeout = payload.step_timeout_seconds or settings.AGENT_STEP_TIMEOUT

    # Gather context for LLM
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

    # Phase 1 — Generate plan
    _jobs[job_id]["status"] = AgentJobStatus.WAITING_FOR_LLM
    _publish_event(job_id, {"type": "status", "status": AgentJobStatus.WAITING_FOR_LLM.value})

    plan: AgentPlan = await agent_llm.generate_plan(
        objective=payload.objective,
        context=context,
        max_steps=payload.max_steps,
    )

    _jobs[job_id]["status"] = AgentJobStatus.RUNNING
    _publish_event(job_id, {"type": "plan", "steps": [s.model_dump() for s in plan.steps]})

    results: list[StepResult] = []
    decisions: list[AgentDecision] = []
    completed_steps: list[int] = []
    remaining_steps: list[AgentStep] = list(plan.steps)
    summary_parts: list[str] = []
    final_success = True

    # Phase 2 — Execute loop
    step_index = 0
    while step_index < len(remaining_steps):
        step = remaining_steps[step_index]
        retry_count = 0
        step_done = False

        while not step_done:
            if payload.dry_run:
                # In dry-run just record a synthetic success
                result = StepResult(
                    step=step.step,
                    tool=step.tool,
                    args=step.args,
                    stdout=f"[dry-run] would execute tool={step.tool.value}",
                    stderr="",
                    exit_code=0,
                    duration_ms=0,
                    executed_at=datetime.now(timezone.utc),
                    success=True,
                )
                decision = AgentDecision(
                    action=DecisionAction.CONTINUE,
                    reason="Dry run — skipping actual execution",
                    summary_so_far="",
                )
            else:
                _publish_event(job_id, {"type": "step_start", "step": step.step, "tool": step.tool.value})

                result = await _execute_tool(
                    step=step,
                    server=server,
                    allow_write=payload.allow_write,
                    timeout=step_timeout,
                )

                _publish_event(
                    job_id,
                    {
                        "type": "step_result",
                        "step": result.step,
                        "success": result.success,
                        "exit_code": result.exit_code,
                    },
                )

                # Ask LLM to evaluate
                _jobs[job_id]["status"] = AgentJobStatus.WAITING_FOR_LLM
                _publish_event(job_id, {"type": "status", "status": AgentJobStatus.WAITING_FOR_LLM.value})

                eval_context = {
                    **context,
                    "previous_steps_summary": " | ".join(summary_parts),
                    "retry_count": retry_count,
                    "max_retries": max_retries,
                }

                decision = await agent_llm.evaluate_step(
                    step=step,
                    result=result,
                    context=eval_context,
                )

                _jobs[job_id]["status"] = AgentJobStatus.RUNNING
                _publish_event(
                    job_id,
                    {"type": "decision", "step": step.step, "action": decision.action.value},
                )

                # Audit every step
                _audit_step(
                    job_id=job_id,
                    user_id=user_id,
                    server_id=payload.server_id,
                    objective=payload.objective,
                    result=result,
                    decision=decision,
                )

            results.append(result)
            decisions.append(decision)

            if decision.summary_so_far:
                summary_parts.append(decision.summary_so_far)

            if decision.action == DecisionAction.CONTINUE:
                completed_steps.append(step.step)
                step_done = True
                step_index += 1

            elif decision.action == DecisionAction.RETRY:
                if retry_count >= max_retries:
                    # Exhausted retries — abort
                    final_success = False
                    _publish_event(job_id, {"type": "abort", "step": step.step, "reason": "max retries exceeded"})
                    step_index = len(remaining_steps)  # exit outer loop
                    step_done = True
                else:
                    retry_count += 1
                    context["failure_history"].append(
                        {"step": step.step, "exit_code": result.exit_code, "stderr": result.stderr[:200]}
                    )

            elif decision.action == DecisionAction.MODIFY:
                if decision.modified_step:
                    remaining_steps[step_index] = decision.modified_step
                    step = decision.modified_step
                    retry_count += 1
                    if retry_count > max_retries:
                        final_success = False
                        step_index = len(remaining_steps)
                        step_done = True
                else:
                    # No modified step provided → treat as abort
                    final_success = False
                    step_index = len(remaining_steps)
                    step_done = True

            elif decision.action == DecisionAction.ABORT:
                final_success = False
                _publish_event(job_id, {"type": "abort", "step": step.step, "reason": decision.reason})
                step_index = len(remaining_steps)
                step_done = True

            else:
                # Unknown action
                final_success = False
                step_index = len(remaining_steps)
                step_done = True

    ok_count = sum(1 for r in results if r.success)
    if payload.dry_run:
        summary = f"Dry-run completed: {len(plan.steps)} step(s) planned, none executed."
    else:
        summary = (
            f"Forge v2 run completed: {len(results)} step(s) executed, "
            f"{ok_count} successful. "
            + (" | ".join(summary_parts) if summary_parts else "")
        ).strip()

    _audit_run_complete(
        job_id=job_id,
        user_id=user_id,
        server_id=payload.server_id,
        objective=payload.objective,
        summary=summary,
        success=final_success,
        dry_run=payload.dry_run,
    )

    _publish_event(job_id, {"type": "completed", "success": final_success, "summary": summary})

    return ForgeV2RunResponse(
        job_id=job_id,
        objective=payload.objective,
        dry_run=payload.dry_run,
        plan=plan.steps,
        results=results,
        decisions=decisions,
        summary=summary,
        success=final_success,
    )


# ---------------------------------------------------------------------------
# Public service API
# ---------------------------------------------------------------------------


class ForgeV2Service:
    """Service layer for Forge v2 agent operations."""

    @staticmethod
    def _check_write_permission(user_email: str, payload: ForgeV2RunRequest) -> None:
        """Restrict allow_write mode to admin emails when admins are configured."""
        settings = get_settings()
        admins = {e.strip().lower() for e in settings.AGENT_ADMIN_EMAILS.split(",") if e.strip()}

        if payload.allow_write and admins and user_email.lower() not in admins:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Write-mode is restricted to agent admins",
            )

    @staticmethod
    async def get_plan(
        payload: ForgeV2RunRequest,
        current_user: dict[str, Any],
    ) -> ForgeV2PlanResponse:
        user_id: str = current_user["sub"]
        user_email = str(current_user.get("email", ""))
        ForgeV2Service._check_write_permission(user_email, payload)

        server = ServerService.get_server(server_id=payload.server_id, user_id=user_id)
        server_metadata = {
            "host": server.get("host"),
            "ssh_user": server.get("ssh_user"),
            "name": server.get("name"),
        }

        job_id = str(uuid4())
        plan = await agent_llm.generate_plan(
            objective=payload.objective,
            context={
                "server_metadata": server_metadata,
                "failure_history": [],
                "allow_write": payload.allow_write,
            },
            max_steps=payload.max_steps,
        )

        return ForgeV2PlanResponse(
            job_id=job_id,
            objective=payload.objective,
            plan=plan.steps,
            context_summary=plan.context_summary,
        )

    @staticmethod
    def submit_job(job_id: str) -> None:
        """Register a new Forge v2 job."""
        _jobs[job_id] = {
            "status": AgentJobStatus.QUEUED,
            "run": None,
            "error": None,
            "events_queue": asyncio.Queue(maxsize=500),
        }

    @staticmethod
    def get_job_status(job_id: str) -> ForgeV2JobResponse:
        job = _jobs.get(job_id)
        if job is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Forge v2 job not found",
            )
        return ForgeV2JobResponse(
            job_id=job_id,
            status=job["status"],
            run=job.get("run"),
            error=job.get("error"),
        )

    @staticmethod
    def get_events_queue(job_id: str) -> asyncio.Queue[dict[str, Any]]:
        job = _jobs.get(job_id)
        if job is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Forge v2 job not found",
            )
        q = job.get("events_queue")
        if q is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="No event stream for this job",
            )
        return q

    @staticmethod
    async def _run_job(
        job_id: str,
        payload: ForgeV2RunRequest,
        current_user: dict[str, Any],
    ) -> None:
        """Background coroutine: run the full agent loop and store result."""
        settings = get_settings()
        # Use module-level semaphore to respect AGENT_MAX_CONCURRENCY
        sem = _get_or_create_semaphore(settings.AGENT_MAX_CONCURRENCY)

        async with sem:
            _jobs[job_id]["status"] = AgentJobStatus.RUNNING
            user_id: str = current_user["sub"]

            try:
                server = ServerService.get_server(
                    server_id=payload.server_id,
                    user_id=user_id,
                )

                run_response = await _run_agent_loop(
                    job_id=job_id,
                    payload=payload,
                    server=server,
                    user_id=user_id,
                )

                _jobs[job_id]["status"] = AgentJobStatus.COMPLETED
                _jobs[job_id]["run"] = run_response

            except HTTPException as exc:
                logger.error("ForgeV2 job %s failed (HTTPException): %s", job_id, exc.detail)
                _jobs[job_id]["status"] = AgentJobStatus.FAILED
                _jobs[job_id]["error"] = exc.detail
                _publish_event(job_id, {"type": "error", "detail": exc.detail})

            except Exception as exc:
                logger.exception("ForgeV2 job %s failed (unexpected): %s", job_id, exc)
                _jobs[job_id]["status"] = AgentJobStatus.FAILED
                _jobs[job_id]["error"] = str(exc)
                _publish_event(job_id, {"type": "error", "detail": str(exc)})

    @staticmethod
    async def run_async(
        job_id: str,
        payload: ForgeV2RunRequest,
        current_user: dict[str, Any],
    ) -> None:
        """Start agent job as a background task (called from router)."""
        await ForgeV2Service._run_job(job_id=job_id, payload=payload, current_user=current_user)


# ---------------------------------------------------------------------------
# Module-level concurrency semaphore
# ---------------------------------------------------------------------------

_semaphore: asyncio.Semaphore | None = None


def _get_or_create_semaphore(max_concurrency: int) -> asyncio.Semaphore:
    global _semaphore
    if _semaphore is None:
        _semaphore = asyncio.Semaphore(max_concurrency)
    return _semaphore
