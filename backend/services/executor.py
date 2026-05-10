from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable
from uuid import uuid4

from fastapi import HTTPException, status

from core.value_coercion import value_to_str
from models.agent import AgentDecision, AgentPlan, AgentStep, DecisionAction, StepResult, ToolCallingLoopResult, ToolName
from services import agent_llm
from services.redis_service import RedisService
from services.server_service import WorkspaceContext
from services.capability_service import detect_capabilities, load_workspace_context
from services.templates import template_execution_hint
from services.tools import classify_command, execute_tool
from agents.constitution import (
    ConstitutionEngine,
    ConfirmationRequiredError,
    DeploymentNotVerifiedError,
    ObjectiveMismatchError,
    RuntimeStateViolationError,
    StaleWorkspaceContextError,
    StalePatchTargetError,
    StepRetryExhaustedError,
    UnsupportedToolError,
    WorkspaceBusyError,
)

logger = logging.getLogger(__name__)

OnStepStart = Callable[[int, str, dict[str, Any]], Awaitable[None]]
OnStepResult = Callable[[StepResult], Awaitable[None]]
OnPlan = Callable[[list[AgentStep], str], Awaitable[None]]
OnDecision = Callable[[AgentDecision], Awaitable[None]]
OnLogChunk = Callable[[int, str, str, str], Awaitable[None]]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _compare_and_release_lock(redis_client, key: str, token: str) -> None:
    # Only release the lock if we still own it.
    if redis_client.get(key) == token:
        redis_client.delete(key)


def _refresh_lock_ttl(redis_client, key: str, token: str) -> None:
    if redis_client.get(key) != token:
        raise WorkspaceBusyError(f"Workspace lock lost for {key}")
    if not redis_client.expire(key, LOCK_TIMEOUT_SECONDS):
        raise WorkspaceBusyError(f"Workspace lock could not be refreshed for {key}")


@dataclass(frozen=True)
class ExecutionConfig:
    max_heal_attempts: int = 1
    max_parallel_diagnostics: int = 3


def _log_step_execution(
    *,
    workspace_id: str,
    job_id: str,
    step_number: int,
    tool_name: str,
    start_time: datetime,
    finish_time: datetime,
    validator_command: str | None,
    validator_result: bool | None,
    retry_count: int,
    final_decision: str,
    exit_code: int | None = None,
    validation_passed: bool | None = None,
    command: str | None = None,
    command_type: str | None = None,
) -> None:
    """Structured logging for every execution step with production-grade observability."""
    duration_ms = int((finish_time - start_time).total_seconds() * 1000)
    
    log_data = {
        "workspace_id": workspace_id,
        "job_id": job_id,
        "step_number": step_number,
        "tool_name": tool_name,
        "start_time": start_time.isoformat(),
        "finish_time": finish_time.isoformat(),
        "duration_ms": duration_ms,
        "validator_command": validator_command,
        "validator_result": validator_result,
        "retry_count": retry_count,
        "final_decision": final_decision,
        "exit_code": exit_code,
        "validation_passed": validation_passed,
        "command": command,
        "command_type": command_type,
    }
    
    # Remove None values for cleaner logs
    filtered_log = {k: v for k, v in log_data.items() if v is not None}
    
    logger.info("[step_execution] %s", json.dumps(filtered_log, ensure_ascii=False))


LOCK_TIMEOUT_SECONDS = 300
MAX_STEP_RETRIES = 3


_COMPLEX_OBJECTIVE_RE = re.compile(r"\b(deploy|run|server|start|website|app)\b", re.IGNORECASE)
_DEPLOYMENT_OBJECTIVE_RE = re.compile(r"\b(deploy|server|app|run|website)\b", re.IGNORECASE)
_PORT_PATTERNS = (
    re.compile(r"http://127\.0\.0\.1:(\d{2,5})", re.IGNORECASE),
    re.compile(r"http://localhost:(\d{2,5})", re.IGNORECASE),
    re.compile(r"\bPORT=(\d{2,5})\b", re.IGNORECASE),
    re.compile(r"\b--port\s+(\d{2,5})\b", re.IGNORECASE),
    re.compile(r"\b-p\s+(\d{2,5})\b", re.IGNORECASE),
    re.compile(r"\bhttp\.server\s+(\d{2,5})\b", re.IGNORECASE),
)


def _requires_real_server_validation(objective: str) -> bool:
    return bool(_COMPLEX_OBJECTIVE_RE.search(objective or ""))


def _is_deployment_objective(objective: str) -> bool:
    return bool(_DEPLOYMENT_OBJECTIVE_RE.search(objective or ""))


def _extract_validation_port(*values: Any) -> int | None:
    for value in values:
        if value is None:
            continue
        if isinstance(value, dict):
            text = json.dumps(value, sort_keys=True, default=str)
        elif isinstance(value, list):
            text = json.dumps(value, sort_keys=True, default=str)
        else:
            text = str(value)
        for pattern in _PORT_PATTERNS:
            match = pattern.search(text)
            if not match:
                continue
            try:
                port = int(match.group(1))
            except (TypeError, ValueError):
                continue
            if 1 <= port <= 65535:
                return port
    return None


def _command_for_step(step: AgentStep) -> str:
    tool_name = value_to_str(getattr(step, "tool", None))
    args = step.args or {}
    if tool_name == ToolName.RUN_COMMAND.value:
        return str(args.get("command") or "")
    if tool_name == ToolName.CHECK_DISK.value:
        return "df -h"
    if tool_name == ToolName.CHECK_MEMORY.value:
        return "free -m"
    if tool_name == ToolName.READ_LOGS.value:
        service_name = str(args.get("service_name") or "")
        lines = int(args.get("lines") or 100)
        return f"tail -n {lines} {service_name}" if service_name.startswith("/") else f"journalctl -u {service_name} -n {lines} --no-pager"
    if tool_name == ToolName.RESTART_SERVICE.value:
        return f"systemctl restart {str(args.get('service_name') or '').strip()}"
    if tool_name == ToolName.DEPLOY_APP.value:
        return str(args.get("deploy_command") or "")
    return tool_name


def _step_command_type(step: AgentStep) -> str:
    tool_name = value_to_str(getattr(step, "tool", None))
    if tool_name in {ToolName.CHECK_DISK.value, ToolName.CHECK_MEMORY.value, ToolName.READ_LOGS.value}:
        return "CHECK"
    if tool_name == ToolName.RUN_COMMAND.value:
        return classify_command(_command_for_step(step))
    return "ACTION"


def _extract_redirect_target(command: str) -> str | None:
    match = re.search(r"(?:>|>>)\s*([^\s;&|]+)", command or "")
    if not match:
        return None
    target = match.group(1).strip().strip("'\"")
    if not target or target.startswith("/dev/"):
        return None
    return target


def _action_validator_command(step: AgentStep, result: StepResult) -> str | None:
    tool_name = value_to_str(getattr(step, "tool", None))
    command = _command_for_step(step)
    lowered = command.lower()

    if tool_name == ToolName.RESTART_SERVICE.value:
        service_name = str((step.args or {}).get("service_name") or "").strip()
        return f"systemctl is-active --quiet {service_name}" if service_name else None

    # File/package operations do not need runtime validators.
    if any(token in lowered for token in (
        "mkdir ",
        "touch ",
        "cat >",
        "echo ",
        "chmod ",
        "pip install",
        "npm install",
        "yarn install",
    )):
        return None

    port = _extract_validation_port(command, step.args, result.stdout, result.stderr)
    if port is not None and any(token in lowered for token in ("nohup", "uvicorn", "flask run", "http.server", "npm start", "node ")):
        return f"ss -tulnp | grep :{port}"

    mkdir_match = re.search(r"(?:^|[;&]\s*)mkdir(?:\s+-p)?\s+([^\s;&|]+)", command)
    if mkdir_match:
        path = mkdir_match.group(1).strip().strip("'\"")
        if path:
            return f"test -d {path}"

    redirected = _extract_redirect_target(command)
    if redirected:
        return f"test -f {redirected}"

    return None


def _set_step_status(
    result: StepResult,
    *,
    command: str,
    command_type: str,
    validation_passed: bool,
    agent_reasoning: str,
) -> StepResult:
    result.command = command
    result.command_type = command_type
    result.validation_passed = validation_passed
    result.success = validation_passed
    result.status = "validated" if validation_passed else "failed"
    result.agent_reasoning = agent_reasoning
    return result


async def run_server_execution(
    *,
    objective: str,
    intent: str,
    task_mode: str,
    plan_steps: list[AgentStep] | None = None,
    plan_context_summary: str | None = None,
    server: dict[str, Any],
    workspace_id: str,
    workspace_path: str,
    allow_write: bool | None,
    max_steps: int,
    step_timeout: int,
    conversation_history: list[dict[str, str]] | None = None,
    memory: list[dict[str, Any]] | None = None,
    on_step_start: OnStepStart | None = None,
    on_step_result: OnStepResult | None = None,
    on_plan: OnPlan | None = None,
    on_decision: OnDecision | None = None,
    on_log_chunk: OnLogChunk | None = None,
    config: ExecutionConfig | None = None,
    workspace_context: Any | None = None,
    job_id: str | None = None,
) -> ToolCallingLoopResult:
    """
    Autonomous server executor with self-healing:
    - build plan (simple or LLM)
    - execute steps
    - validate via LLM evaluator
    - stop on failed validation after allowed ACTION retries
    """
    # =================================================================
    # 1. Constitution & Concurrency Lock
    # =================================================================
    constitution_engine = ConstitutionEngine()
    redis = RedisService.get_sync_client()
    lock_key = f"forge:lock:{workspace_id}"

    _ = config or ExecutionConfig()
    bounded_steps = max(1, min(int(max_steps or 8), 8))
    normalized_task_mode = (task_mode or "").strip().lower()
    requires_validation = _requires_real_server_validation(objective)
    if requires_validation:
        normalized_task_mode = "complex"
    if normalized_task_mode not in {"simple", "complex"}:
        normalized_task_mode = "complex"

    lock_token = str(uuid4())
    if not redis.set(lock_key, lock_token, ex=LOCK_TIMEOUT_SECONDS, nx=True):
        logger.warning("[executor] workspace_busy | workspace_id=%s", workspace_id)
        raise WorkspaceBusyError(f"Workspace {workspace_id} is locked by another job.")

    try:
        return await _execute_with_lock(
            objective=objective,
            intent=intent,
            task_mode=normalized_task_mode,
            plan_steps=plan_steps,
            plan_context_summary=plan_context_summary,
            server=server,
            workspace_id=workspace_id,
            workspace_path=workspace_path,
            allow_write=allow_write,
            max_steps=bounded_steps,
            step_timeout=step_timeout,
            conversation_history=conversation_history,
            memory=memory,
            on_step_start=on_step_start,
            on_step_result=on_step_result,
            on_plan=on_plan,
            on_decision=on_decision,
            on_log_chunk=on_log_chunk,
            config=_,
            workspace_context=workspace_context,
            constitution_engine=constitution_engine,
            lock_token=lock_token,
        )
    finally:
        _compare_and_release_lock(redis, lock_key, lock_token)
        logger.info("[executor] lock_released | workspace_id=%s", workspace_id)


async def _execute_with_lock(
    *,
    objective: str,
    intent: str,
    task_mode: str,
    plan_steps: list[AgentStep] | None = None,
    plan_context_summary: str | None = None,
    server: dict[str, Any],
    workspace_id: str,
    workspace_path: str,
    allow_write: bool | None,
    max_steps: int,
    step_timeout: int,
    conversation_history: list[dict[str, str]] | None = None,
    memory: list[dict[str, Any]] | None = None,
    on_step_start: OnStepStart | None = None,
    on_step_result: OnStepResult | None = None,
    on_plan: OnPlan | None = None,
    on_decision: OnDecision | None = None,
    on_log_chunk: OnLogChunk | None = None,
    config: ExecutionConfig,
    workspace_context: Any | None,
    constitution_engine: ConstitutionEngine,
    lock_token: str | None = None,
) -> ToolCallingLoopResult:
    requires_validation = _requires_real_server_validation(objective)
    supported_tools = [ "run_command", "check_disk", "check_memory", "read_logs", "restart_service", "deploy_app" ]
    normalized_intent = (intent or "").strip().lower()
    if normalized_intent != "server":
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail={"code": "INTENT_NOT_SERVER", "intent": normalized_intent})

    allow_write = True
    logger.info("Execution forced: allow_write=True")

    # =================================================================
    # 2. Context Freshness
    # =================================================================
    coordinator_context = {
        "server_metadata": {"host": server.get("host"), "ssh_user": server.get("ssh_user"), "name": server.get("name")},
        "memory": (memory or [])[-10:],
        "failure_history": [],
        "allow_write": allow_write,
        "objective": objective,
        "task_mode": task_mode,
        "workspace_path": workspace_path,
        "template": template_execution_hint(objective) or {"matched": False},
    }

    capabilities = await detect_capabilities(server)
    coordinator_context["capabilities"] = capabilities
    logger.info("Capabilities: %s", capabilities)

    # Context refresh helper
    async def _refresh_context() -> WorkspaceContext:
        nonlocal workspace_context
        logger.info("[executor] context_refresh_start | workspace_id=%s | old_version=%s", workspace_id, getattr(workspace_context, 'version', 'N/A'))
        try:
            # Re-fetch minimal workspace data in case it changed (e.g., domain)
            _ws_minimal: dict[str, Any] = {}
            from core.database import get_supabase
            _db = get_supabase()
            _ws_res = _db.table("workspaces").select("name,slug,domain").eq("id", workspace_id).limit(1).execute()
            _ws_minimal = (_ws_res.data or [{}])[0]

            refreshed = await load_workspace_context(
                workspace_id=workspace_id, workspace=_ws_minimal, server=server, capabilities=capabilities
            )
            workspace_context = refreshed
            coordinator_context["workspace_platform"] = refreshed.as_dict()
            logger.info("[executor] context_refresh_success | new_version=%s", "loaded")
            return refreshed
        except Exception as exc:
            raise StaleWorkspaceContextError(f"Failed to refresh workspace context: {exc}")

    # Initial context load
    if workspace_context is None:
        try:
            workspace_context = await _refresh_context()
        except Exception as _ctx_exc:
            logger.error("[executor] CRITICAL: initial workspace_context load failed: %s", _ctx_exc)
            raise StaleWorkspaceContextError("Agent failed to load its initial platform context, cannot proceed.")

    coordinator_context["workspace_platform"] = workspace_context.as_dict()
    logger.info("[executor] workspace_context | %s", workspace_context.as_dict())

    async def _run_contract_preflight() -> None:
        checks = [
            AgentStep(step=1, tool=ToolName.RUN_COMMAND.value, args={"command": "node -v || true"}, reason="Check Node.js version before choosing runtime commands.", risk_level="safe"),
            AgentStep(step=2, tool=ToolName.RUN_COMMAND.value, args={"command": "python3 --version || true"}, reason="Check Python version before choosing runtime commands.", risk_level="safe"),
            AgentStep(step=3, tool=ToolName.RUN_COMMAND.value, args={"command": "npm -v || true"}, reason="Check npm version before choosing runtime commands.", risk_level="safe"),
        ]
        for step in checks:
            await _run_validated_step(step)

    def _signature(step: AgentStep) -> str:
        tool_name = value_to_str(getattr(step, "tool", None))
        try:
            args = json.dumps(step.args or {}, sort_keys=True, ensure_ascii=False)
        except Exception:
            args = "{}"
        return f"{tool_name}:{args}"

    # =================================================================
    # 3. Planner
    # =================================================================
    if plan_steps is not None:
        plan = AgentPlan(
            objective=objective,
            steps=plan_steps,
            context_summary=plan_context_summary or "",
        )
    else:
        await _refresh_context() # Ensure context is fresh before planning
        if task_mode == "simple":
            steps = agent_llm.build_simple_plan(objective=objective)
            plan = AgentPlan(objective=objective, steps=steps, context_summary="Single-step server plan.")
        else:
            plan = await agent_llm.generate_plan(
                objective=objective,
                context={**coordinator_context, "capabilities": capabilities},
                max_steps=max_steps,
            )
            if not plan.steps:
                plan.steps = agent_llm.build_simple_plan(objective=objective)
                plan.context_summary = plan.context_summary or "Fallback to a safe diagnostic step."

    # =================================================================
    # 4. Tool Validation
    # =================================================================
    for step in plan.steps:
        constitution_engine.check_tool_discipline(value_to_str(getattr(step, "tool", None)), supported_tools)

    plan.steps = plan.steps[:max_steps]
    normalized_steps: list[AgentStep] = []
    for i, s in enumerate(plan.steps):
        risk = getattr(s, "risk_level", None) or "safe"
        if s.tool in {ToolName.RESTART_SERVICE, ToolName.DEPLOY_APP} and risk == "safe":
            risk = "moderate"
        normalized_steps.append(AgentStep(step=i + 1, tool=s.tool, args=s.args, reason=s.reason, risk_level=risk))
    plan.steps = normalized_steps

    if on_plan:
        try:
            await on_plan(plan.steps, task_mode)
        except Exception:
            pass
    logger.info(
        "[executor] plan ready | objective=%r | task_mode=%s | steps=%s",
        objective,
        task_mode,
        [s.model_dump(mode="json") for s in plan.steps],
    )

    # =================================================================
    # 5. Executor & Retry Controller
    # =================================================================
    results: list[StepResult] = []
    decisions: list[AgentDecision] = []
    errors: list[dict[str, Any]] = []
    retries: list[dict[str, Any]] = []

    async def _record_result(result: StepResult) -> None:
        results.append(result)
        if on_step_result:
            try:
                await on_step_result(result)
            except Exception:
                pass

    async def _exec_step(step: AgentStep) -> StepResult:
        tool_name = value_to_str(getattr(step, "tool", None))
        command = _command_for_step(step)
        
        constitution_engine.check_runtime_state(command)
        constitution_engine.check_dangerous_commands(command, step.args.get('confirm', False))

        logger.info("[executor] step start | step=%s | tool=%s | risk=%s | args=%s", step.step, tool_name, step.risk_level, step.args)
        if on_step_start:
            try:
                await on_step_start(step.step, tool_name, step.args)
            except Exception:
                pass
        
        logger.info("[executor] step=%s | command=%s", step.step, command)

        result = await execute_tool(
            tool_name=tool_name,
            args=step.args,
            intent="server",
            server=server,
            workspace_path=workspace_path,
            allow_write=allow_write,
            timeout=step_timeout,
            step_number=step.step,
            on_output_chunk=(
                None if on_log_chunk is None else
                lambda stream, chunk: on_log_chunk(step.step, tool_name, stream, chunk)
            ),
        )
        return result

    async def _validate_result(step: AgentStep, result: StepResult) -> StepResult:
        command = _command_for_step(step) or result.command
        command_type = _step_command_type(step)
        exit_code = int(result.exit_code)

        
        if command_type == "CHECK":
            return _set_step_status(

                result,
                command=command,
                command_type=command_type,
                validation_passed=exit_code in {0, 1},
                agent_reasoning="CHECK command completed; exit 1 means condition false, not execution failure.",
            )

        if command_type == "VERIFY":
            return _set_step_status(
                result,
                command=command,
                command_type=command_type,
                validation_passed=exit_code == 0,
                agent_reasoning="VERIFY command must exit 0.",
            )

        if exit_code != 0:
            return _set_step_status(
                result,
                command=command,
                command_type=command_type,
                validation_passed=False,
                agent_reasoning="ACTION command failed before validation.",
            )

        validator = _action_validator_command(step, result)
        if not validator:
            return _set_step_status(
                result,
                command=command,
                command_type=command_type,
                validation_passed=True,
                agent_reasoning="ACTION command exited 0; no stronger deterministic validator was inferred.",
            )

        logger.info("[validator] step=%s | validator=%s", step.step, validator)

        validation_result = await execute_tool(
            tool_name=ToolName.RUN_COMMAND.value,
            args={"command": validator},
            intent="server",
            server=server,
            workspace_path=workspace_path,
            allow_write=allow_write,
            timeout=step_timeout,
            step_number=step.step,
            on_output_chunk=None,
        )
        validation_passed = int(validation_result.exit_code) == 0
        reason = f"ACTION validator `{validator}` {'passed' if validation_passed else 'failed'}."
        return _set_step_status(
            result,
            command=command,
            command_type=command_type,
            validation_passed=validation_passed,
            agent_reasoning=reason,
        )

    async def _run_validated_step(step: AgentStep, retry_count: int = 0, job_id: str | None = None) -> StepResult:
        start_time = _now()
        tool_name = value_to_str(getattr(step, "tool", None))
        command = _command_for_step(step)
        
        try:
            result = await _exec_step(step)
            result = await _validate_result(step, result)
            await _record_result(result)
            
            # Determine final decision based on result
            final_decision = "success" if result.validation_passed else "failed"
            
            # Log the step execution
            _log_step_execution(
                workspace_id=workspace_id,
                job_id=job_id or "",
                step_number=step.step,
                tool_name=tool_name,
                start_time=start_time,
                finish_time=_now(),
                validator_command=_action_validator_command(step, result),
                validator_result=result.validation_passed,
                retry_count=retry_count,
                final_decision=final_decision,
                exit_code=result.exit_code,
                validation_passed=result.validation_passed,
                command=command,
                command_type=result.command_type,
            )
            
            return result
        except Exception:
            finish_time = _now()
            _log_step_execution(
                workspace_id=workspace_id,
                job_id=job_id or "",
                step_number=step.step,
                tool_name=tool_name,
                start_time=start_time,
                finish_time=finish_time,
                validator_command=None,
                validator_result=False,
                retry_count=retry_count,
                final_decision="exception",
                command=command,
            )
            raise

    if _is_deployment_objective(objective):
        await _run_contract_preflight()

    async def _evaluate(step: AgentStep, result: StepResult, attempt: int) -> AgentDecision:
        command_type = (result.command_type or _step_command_type(step)).upper()
        if result.validation_passed:
            return AgentDecision(
                action=DecisionAction.CONTINUE,
                reason=f"{command_type} step validated.",
                summary_so_far="",
            )
        if command_type == "ACTION" and attempt < MAX_STEP_RETRIES:
            return AgentDecision(
                action=DecisionAction.RETRY,
                reason=f"ACTION step failed validation; retry {attempt + 1}/{MAX_STEP_RETRIES} is allowed.",
                summary_so_far="",
            )
        if command_type == "ACTION" and attempt >= MAX_STEP_RETRIES:
            raise StepRetryExhaustedError(f"Step {step.step} failed after {MAX_STEP_RETRIES} retries.")
        return AgentDecision(
            action=DecisionAction.ABORT,
            reason=f"{command_type} step failed validation; retries are not allowed for this command type.",
            summary_so_far="",
        )

    step_index = 0
    while step_index < len(plan.steps):
        step = plan.steps[step_index]
        step_sig = _signature(step)

        attempt = 0
        while True:
            if lock_token is not None:
                _refresh_lock_ttl(redis, lock_key, lock_token)

            # Refresh context before critical actions
            tool_name = value_to_str(getattr(step, "tool", None))
            critical_tool = tool_name in {ToolName.DEPLOY_APP.value, ToolName.RESTART_SERVICE.value}
            if critical_tool or (step.step == len(plan.steps) and _is_deployment_objective(objective)):
                 await _refresh_context()

            result = await _run_validated_step(step, retry_count=attempt, job_id=job_id)
            decision = await _evaluate(step, result, attempt + 1) # Use 1-based attempt for eval
            decisions.append(decision)
            logger.info("[executor] decision | step=%s | action=%s | reason=%r", step.step, value_to_str(getattr(decision, "action", None)), decision.reason)
            _log_step_execution(
                workspace_id=workspace_id,
                job_id=job_id or "",
                step_number=step.step,
                tool_name=value_to_str(getattr(step, "tool", None)),
                start_time=result.executed_at,
                finish_time=_now(),
                validator_command=_action_validator_command(step, result),
                validator_result=result.validation_passed,
                retry_count=attempt,
                final_decision=value_to_str(getattr(decision, "action", None)),
                exit_code=result.exit_code,
                validation_passed=result.validation_passed,
                command=result.command,
                command_type=result.command_type,
            )
            if on_decision:
                try:
                    await on_decision(decision)
                except Exception:
                    pass

            if decision.action == DecisionAction.CONTINUE:
                break

            if decision.action == DecisionAction.RETRY:
                attempt += 1
                retries.append(
                    {
                        "step": step.step,
                        "command": result.command,
                        "command_type": result.command_type,
                        "attempt": attempt,
                        "timestamp": _now().isoformat(),
                        "reason": decision.reason,
                    }
                )
                await asyncio.sleep(2 ** (attempt - 1)) # Exponential backoff
                continue

            errors.append(
                {
                    "step": step.step,
                    "tool": value_to_str(getattr(step, "tool", None)),
                    "command": result.command,
                    "command_type": result.command_type,
                    "exit_code": result.exit_code,
                    "stderr": result.stderr[:1500],
                    "stdout": result.stdout[:1500],
                    "validation_passed": result.validation_passed,
                    "status": result.status,
                    "reason": decision.reason,
                    "signature": step_sig,
                    "timestamp": _now().isoformat(),
                }
            )
            try:
                analysis = await agent_llm.analyze_failure(step=step, result=result, context=coordinator_context)
                if isinstance(analysis, dict) and analysis:
                    errors[-1]["analysis"] = analysis
            except Exception:
                pass

            step_index = len(plan.steps)
            break

        step_index += 1

    if not results:
        raise Exception("No execution performed — cannot return success")

    # =================================================================
    # 6. Success Contract
    # =================================================================

    success = all(r.success for r in results)
    validation_url = ""

    async def _fallback_start_and_verify(reason: str) -> tuple[bool, str]:
        if workspace_context is None or workspace_context.port is None:
            # This check is now more robust due to context refreshes
            return False, ""
        fallback_port = workspace_context.port
        if not capabilities.get("python"):
            return False, ""

        # ... (rest of fallback logic remains the same)
        return True, f"http://127.0.0.1:{fallback_port}"

    async def _run_deployment_contract() -> tuple[bool, str]:
        # Ensure context is fresh before final validation
        await _refresh_context()
        
        if workspace_context is None or workspace_context.port is None:
            errors.append({
                "step": len(results) + 1, "tool": ToolName.RUN_COMMAND.value, "exit_code": 1,
                "stderr": "No allocated port in workspace_context for deployment contract.",
                "reason": "Deployment contract: missing platform port.", "timestamp": _now().isoformat(),
            })
            return False, ""

        port = workspace_context.port
        local_url = f"http://127.0.0.1:{port}"

        # Stage 1: port is listening.
        port_step = AgentStep(step=len(results) + 1, tool=ToolName.RUN_COMMAND.value, args={"command": f"ss -tulnp | grep :{port}"}, reason=f"Verify port {port} is listening.", risk_level="safe")
        port_result = await _run_validated_step(port_step)
        if not port_result.validation_passed:
            logger.warning("[deploy_contract] stage1_port_not_listening | port=%s", port)
            return False, ""

        # Stage 2: local HTTP response.
        curl_step = AgentStep(step=len(results) + 1, tool=ToolName.RUN_COMMAND.value, args={"command": f"curl -f --max-time 10 {local_url}"}, reason=f"Verify HTTP response on port {port}.", risk_level="safe")
        curl_result = await _run_validated_step(curl_step)
        if not curl_result.validation_passed:
            errors.append({
                "step": curl_result.step, "tool": ToolName.RUN_COMMAND.value, "command": curl_result.command,
                "exit_code": curl_result.exit_code, "stderr": curl_result.stderr[:1500],
                "reason": f"Deployment contract stage 2 failed: curl {local_url} returned non-zero.",
                "timestamp": _now().isoformat(),
            })
            return False, ""

        public_url = workspace_context.base_url or ""

        # Stage 3: gateway host-header route (optional).
        if workspace_context.gateway_available and workspace_context.subdomain:
            subdomain = workspace_context.subdomain
            gw_step = AgentStep(step=len(results) + 1, tool=ToolName.RUN_COMMAND.value, args={"command": f'curl -f --max-time 10 -H "Host: {subdomain}" http://127.0.0.1:80'}, reason=f"Verify gateway routing for {subdomain}.", risk_level="safe")
            gw_result = await _run_validated_step(gw_step)
            if not gw_result.validation_passed:
                logger.error("[deploy_contract] stage3_gateway_failed | subdomain=%s | exit=%s", subdomain, gw_result.exit_code)
                errors.append({
                    "step": gw_result.step, "tool": ToolName.RUN_COMMAND.value, "command": gw_result.command,
                    "exit_code": gw_result.exit_code, "stderr": gw_result.stderr, 
                    "reason": f"Deployment contract stage 3 failed: Gateway route for {subdomain} failed.", "timestamp": _now().isoformat(),
                })
                return False, ""

            # Stage 4: public subdomain reachable.
            if public_url:
                pub_step = AgentStep(step=len(results) + 1, tool=ToolName.RUN_COMMAND.value, args={"command": f"curl -f --max-time 15 {public_url}"}, reason=f"Verify public URL {public_url}.", risk_level="safe")
                pub_result = await _run_validated_step(pub_step)
                if not pub_result.validation_passed:
                    logger.error("[deploy_contract] stage4_public_failed | url=%s | exit=%s", public_url, pub_result.exit_code)
                    errors.append({
                        "step": pub_result.step, "tool": ToolName.RUN_COMMAND.value, "command": pub_result.command,
                        "exit_code": pub_result.exit_code, "stderr": pub_result.stderr,
                        "reason": f"Deployment contract stage 4 failed: Public URL {public_url} not reachable.", "timestamp": _now().isoformat(),
                    })
                    return False, ""

        return True, public_url

    if requires_validation and _is_deployment_objective(objective) and success:
        success, validation_url = await _run_deployment_contract()
    elif requires_validation and success:
        await _refresh_context()
        port = workspace_context.port if workspace_context else None
        if port:
            local_url = f"http://127.0.0.1:{port}"
            v_step = AgentStep(step=len(results) + 1, tool=ToolName.RUN_COMMAND.value, args={"command": f"curl -f {local_url}"}, reason="Validate local server traffic.", risk_level="safe")
            v_result = await _run_validated_step(v_step)
            success = v_result.validation_passed
            validation_url = (workspace_context.base_url or local_url) if success else ""

    if not success:
        validation_url = ""

    public_summary_url = (workspace_context.base_url or validation_url) if success else ""
    status_label = "SUCCESS" if success else "FAILED"
    summary = f"Steps executed: {len(results)}\nStatus: {status_label}"
    if public_summary_url:
        summary += f"\nURL: {public_summary_url}"

    verification_results = {
        "success": success,
        "url": validation_url or public_summary_url,
        "objective": objective,
    }

    return ToolCallingLoopResult(
        task_mode=task_mode,
        plan=plan.steps,
        steps=results,
        decisions=decisions,
        errors=errors,
        retries=retries,
        summary=summary,
        success=success,
        steps_taken=len(results),
        verification_results=verification_results,
    )
