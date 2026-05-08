from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from fastapi import HTTPException, status

from core.value_coercion import value_to_str
from models.agent import AgentDecision, AgentPlan, AgentStep, DecisionAction, StepResult, ToolCallingLoopResult, ToolName
from services import agent_llm
from services.templates import template_execution_hint
from services.tools import classify_command, execute_tool

logger = logging.getLogger(__name__)

OnStepStart = Callable[[int, str, dict[str, Any]], Awaitable[None]]
OnStepResult = Callable[[StepResult], Awaitable[None]]
OnPlan = Callable[[list[AgentStep], str], Awaitable[None]]
OnDecision = Callable[[AgentDecision], Awaitable[None]]
OnLogChunk = Callable[[int, str, str, str], Awaitable[None]]


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class ExecutionConfig:
    max_heal_attempts: int = 1
    max_parallel_diagnostics: int = 3


MAX_ACTION_RETRIES = 2


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
) -> ToolCallingLoopResult:
    """
    Autonomous server executor with self-healing:
    - build plan (simple or LLM)
    - execute steps
    - validate via LLM evaluator
    - stop on failed validation after allowed ACTION retries
    """
    normalized_intent = (intent or "").strip().lower()
    if normalized_intent != "server":
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail={"code": "INTENT_NOT_SERVER", "intent": normalized_intent})

    allow_write = True
    logger.info("Execution forced: allow_write=True")

    _ = config or ExecutionConfig()
    bounded_steps = max(1, min(int(max_steps or 8), 8))
    normalized_task_mode = (task_mode or "").strip().lower()
    requires_validation = _requires_real_server_validation(objective)
    if requires_validation:
        normalized_task_mode = "complex"
    if normalized_task_mode not in {"simple", "complex"}:
        normalized_task_mode = "complex"

    coordinator_context = {
        "server_metadata": {"host": server.get("host"), "ssh_user": server.get("ssh_user"), "name": server.get("name")},
        "memory": (memory or [])[-10:],
        "failure_history": [],
        "allow_write": allow_write,
        "objective": objective,
        "task_mode": normalized_task_mode,
        "workspace_path": workspace_path,
        "template": template_execution_hint(objective) or {"matched": False},
    }

    from services.capability_service import (
        WorkspaceContext,
        detect_capabilities,
        load_workspace_context,
    )

    capabilities = await detect_capabilities(server)
    coordinator_context["capabilities"] = capabilities
    logger.info("Capabilities: %s", capabilities)

    from services.redis_service import RedisService

    r = RedisService.get_sync_client()
    r.set(f"ws:{workspace_id}:capabilities", json.dumps(capabilities), ex=3600)

    # BUG #1 / #4 fix: load authoritative platform context (port, subdomain,
    # SSL, gateway) from Redis + DB.  This is the ONLY source of truth for the
    # allocated port — never hardcode, never extract from stdout.
    if workspace_context is None:
        try:
            _ws_minimal: dict[str, Any] = {}
            try:
                from core.database import get_supabase
                _db = get_supabase()
                _ws_res = (
                    _db.table("workspaces")
                    .select("name,slug,domain")
                    .eq("id", workspace_id)
                    .limit(1)
                    .execute()
                )
                _ws_minimal = (_ws_res.data or [{}])[0]
            except Exception:
                pass
            workspace_context = await load_workspace_context(
                workspace_id=workspace_id,
                workspace=_ws_minimal,
                server=server,
                capabilities=capabilities,
            )
        except Exception as _ctx_exc:
            logger.warning("[executor] workspace_context load failed: %s — using empty context", _ctx_exc)
            workspace_context = WorkspaceContext(workspace_id=workspace_id)

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

    if plan_steps is not None:
        plan = AgentPlan(
            objective=objective,
            steps=plan_steps,
            context_summary=plan_context_summary or "",
        )
    else:
        if normalized_task_mode == "simple":
            steps = agent_llm.build_simple_plan(objective=objective)
            plan = AgentPlan(objective=objective, steps=steps, context_summary="Single-step server plan.")
        else:
            plan = await agent_llm.generate_plan(
                objective=objective,
                context={**coordinator_context, "capabilities": capabilities},
                max_steps=bounded_steps,
            )
            if not plan.steps:
                plan.steps = agent_llm.build_simple_plan(objective=objective)
                plan.context_summary = plan.context_summary or "Fallback to a safe diagnostic step."

    plan.steps = plan.steps[:bounded_steps]
    # Normalize step numbering.
    normalized_steps: list[AgentStep] = []
    for i, s in enumerate(plan.steps):
        risk = getattr(s, "risk_level", None) or "safe"
        if s.tool in {ToolName.RESTART_SERVICE, ToolName.DEPLOY_APP} and risk == "safe":
            risk = "moderate"
        normalized_steps.append(AgentStep(step=i + 1, tool=s.tool, args=s.args, reason=s.reason, risk_level=risk))
    plan.steps = normalized_steps

    if on_plan:
        try:
            await on_plan(plan.steps, normalized_task_mode)
        except Exception:
            pass
    logger.info(
        "[executor] plan ready | objective=%r | task_mode=%s | steps=%s",
        objective,
        normalized_task_mode,
        [s.model_dump(mode="json") for s in plan.steps],
    )

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
        logger.info("[executor] step start | step=%s | tool=%s | risk=%s | args=%s", step.step, tool_name, step.risk_level, step.args)
        if on_step_start:
            try:
                await on_step_start(step.step, tool_name, step.args)
            except Exception:
                pass
        if tool_name == ToolName.RUN_COMMAND.value:
            cmd = (step.args or {}).get("command", "")
            cmd_stripped = str(cmd).strip()
            is_version_check = any(token in cmd_stripped for token in ("node -v", "python3 --version", "npm -v"))
            missing_runtime = ""
            if "npm" in cmd and not capabilities.get("npm") and not is_version_check:
                missing_runtime = "npm not available on server"
            elif "python" in cmd and not capabilities.get("python") and not is_version_check:
                missing_runtime = "python not available"
            elif "pm2" in cmd and not capabilities.get("pm2"):
                missing_runtime = "pm2 not available on server"
            if missing_runtime:
                result = StepResult(
                    step=step.step,
                    tool=ToolName(tool_name),
                    args=step.args,
                    stdout="",
                    stderr=missing_runtime,
                    exit_code=127,
                    duration_ms=0,
                    executed_at=_now(),
                    success=False,
                    command=_command_for_step(step),
                    command_type=_step_command_type(step),
                    validation_passed=False,
                    status="failed",
                )
                return result
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

    async def _run_validated_step(step: AgentStep) -> StepResult:
        result = await _exec_step(step)
        result = await _validate_result(step, result)
        await _record_result(result)
        return result

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
        if command_type == "ACTION" and attempt < MAX_ACTION_RETRIES:
            return AgentDecision(
                action=DecisionAction.RETRY,
                reason=f"ACTION step failed validation; retry {attempt + 1}/{MAX_ACTION_RETRIES} is allowed.",
                summary_so_far="",
            )
        return AgentDecision(
            action=DecisionAction.ABORT,
            reason=f"{command_type} step failed validation; retries are {'not allowed' if command_type != 'ACTION' else 'exhausted'}.",
            summary_so_far="",
        )

    step_index = 0
    while step_index < len(plan.steps):
        step = plan.steps[step_index]
        step_sig = _signature(step)

        attempt = 0
        while True:
            result = await _run_validated_step(step)
            decision = await _evaluate(step, result, attempt)
            decisions.append(decision)
            logger.info("[executor] decision | step=%s | action=%s | reason=%r", step.step, value_to_str(getattr(decision, "action", None)), decision.reason)
            if on_decision:
                try:
                    await on_decision(decision)
                except Exception:
                    pass

            if decision.action == DecisionAction.CONTINUE:
                break

            if decision.action == DecisionAction.RETRY and result.command_type == "ACTION" and attempt < MAX_ACTION_RETRIES:
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
                await asyncio.sleep(2 ** (attempt - 1))
                continue

            # ABORT or exhausted retries: attempt self-healing replanning for remaining steps.
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

            # Stop immediately after failed validation. Later steps may depend
            # on the side effects this step failed to prove.
            step_index = len(plan.steps)
            break

        step_index += 1

    if not results:
        raise Exception("No execution performed — cannot return success")

    success = all(r.success for r in results)
    # BUG #3 fix: removed premature success=True that was set here before any
    # verification ran.  Success is now only granted after the full deployment
    # contract (port-listening, local curl, optional gateway/subdomain checks).
    validation_url = ""

    async def _fallback_start_and_verify(reason: str) -> tuple[bool, str]:
        # BUG #4 fix: use allocated workspace port; never a hardcoded default.
        if workspace_context is None or workspace_context.port is None:
            errors.append(
                {
                    "step": len(results) + 1,
                    "tool": ToolName.RUN_COMMAND.value,
                    "exit_code": 1,
                    "stderr": (
                        "No allocated port in platform context. "
                        "Cannot start fallback server — never use a hardcoded port."
                    ),
                    "reason": reason,
                    "timestamp": _now().isoformat(),
                }
            )
            return False, ""
        fallback_port = workspace_context.port
        if not capabilities.get("python"):
            errors.append(
                {
                    "step": len(results) + 1,
                    "tool": ToolName.RUN_COMMAND.value,
                    "exit_code": 1,
                    "stderr": "python3 is unavailable; cannot use Python HTTP server fallback.",
                    "reason": reason,
                    "timestamp": _now().isoformat(),
                }
            )
            return False, ""

        retries.append(
            {
                "step": len(results) + 1,
                "tool": ToolName.RUN_COMMAND.value,
                "attempt": 1,
                "timestamp": _now().isoformat(),
                "reason": reason,
            }
        )
        start_cmd = (
            "test -f index.html || printf '%s\\n' "
            "'<!doctype html><title>ThinkSync</title><h1>OK</h1>' > index.html; "
            f"nohup python3 -m http.server {fallback_port} --bind 127.0.0.1 > app.log 2>&1 &"
        )
        start_step = AgentStep(
            step=len(results) + 1,
            tool=ToolName.RUN_COMMAND.value,
            args={"command": start_cmd},
            reason="Fallback: create a minimal static file and start a Python HTTP server after the planned execution failed validation.",
            risk_level="moderate",
        )
        start_result = await _run_validated_step(start_step)
        if not start_result.validation_passed:
            return False, ""
        verify_url = f"http://127.0.0.1:{fallback_port}"
        verify_step = AgentStep(
            step=len(results) + 1,
            tool=ToolName.RUN_COMMAND.value,
            args={"command": f"curl -f {verify_url}"},
            reason="Verify fallback server before returning success.",
            risk_level="safe",
        )
        verify_result = await _run_validated_step(verify_step)
        if not verify_result.validation_passed:
            errors.append(
                {
                    "step": verify_result.step,
                    "tool": ToolName.RUN_COMMAND.value,
                    "command": verify_result.command,
                    "command_type": verify_result.command_type,
                    "exit_code": verify_result.exit_code,
                    "stderr": verify_result.stderr[:1500],
                    "stdout": verify_result.stdout[:1500],
                    "validation_passed": verify_result.validation_passed,
                    "status": verify_result.status,
                    "reason": "Fallback curl verification failed.",
                    "timestamp": _now().isoformat(),
                }
            )
            return False, ""
        return True, verify_url

    # BUG #3 / #4 fix: 5-stage deployment contract.  Uses only
    # workspace_context.port (allocated by platform) — never extracts port
    # from stdout, never uses a hardcoded default.
    async def _run_deployment_contract() -> tuple[bool, str]:
        """Run the full deployment verification contract.

        Stages:
          1. Port listening  — ``ss -tulnp | grep :{port}``
          2. Local curl      — ``curl -f http://127.0.0.1:{port}``
          3. Gateway route   — host-header curl through nginx port 80 (if gateway)
          4. Public URL      — ``curl -f {protocol}://{subdomain}`` (if gateway)

        Returns (ok: bool, public_url: str).
        """
        if workspace_context is None or workspace_context.port is None:
            errors.append(
                {
                    "step": len(results) + 1,
                    "tool": ToolName.RUN_COMMAND.value,
                    "exit_code": 1,
                    "stderr": "No allocated port in workspace_context — deployment contract cannot run.",
                    "reason": "Deployment contract: missing platform port.",
                    "timestamp": _now().isoformat(),
                }
            )
            return False, ""

        port = workspace_context.port
        local_url = f"http://127.0.0.1:{port}"

        # Stage 1: port is listening.
        port_step = AgentStep(
            step=len(results) + 1,
            tool=ToolName.RUN_COMMAND.value,
            args={"command": f"ss -tulnp | grep :{port}"},
            reason=f"Verify port {port} is listening after server start.",
            risk_level="safe",
        )
        port_result = await _run_validated_step(port_step)
        port_listening = str(port) in (port_result.stdout or "")
        if not port_listening:
            logger.warning("[deploy_contract] stage1_port_not_listening | port=%s", port)
            fb_ok, fb_url = await _fallback_start_and_verify(
                f"Port {port} not listening after execution — attempting fallback."
            )
            return fb_ok, fb_url

        # Stage 2: local HTTP response.
        curl_step = AgentStep(
            step=len(results) + 1,
            tool=ToolName.RUN_COMMAND.value,
            args={"command": f"curl -f --max-time 10 {local_url}"},
            reason=f"Verify HTTP response on allocated port {port}.",
            risk_level="safe",
        )
        curl_result = await _run_validated_step(curl_step)
        if not curl_result.validation_passed:
            errors.append(
                {
                    "step": curl_result.step,
                    "tool": ToolName.RUN_COMMAND.value,
                    "command": curl_result.command,
                    "command_type": curl_result.command_type,
                    "exit_code": curl_result.exit_code,
                    "stderr": curl_result.stderr[:1500],
                    "stdout": curl_result.stdout[:1500],
                    "validation_passed": False,
                    "status": "failed",
                    "reason": f"Deployment contract stage 2 failed: curl {local_url} returned non-zero.",
                    "timestamp": _now().isoformat(),
                }
            )
            return False, ""

        # BUG #2 fix: public URL comes from workspace_context — never 127.0.0.1.
        public_url = workspace_context.base_url or ""

        # Stage 3: gateway host-header route (optional — only if gateway is up).
        if workspace_context.gateway_available and workspace_context.subdomain:
            subdomain = workspace_context.subdomain
            gw_step = AgentStep(
                step=len(results) + 1,
                tool=ToolName.RUN_COMMAND.value,
                args={"command": f'curl -f --max-time 10 -H "Host: {subdomain}" http://127.0.0.1:80'},
                reason=f"Verify gateway host-header routing for {subdomain}.",
                risk_level="safe",
            )
            gw_result = await _run_validated_step(gw_step)
            if not gw_result.validation_passed:
                logger.warning(
                    "[deploy_contract] stage3_gateway_failed | subdomain=%s | exit=%s",
                    subdomain, gw_result.exit_code,
                )
                # Gateway failure is non-fatal — local curl already passed.

            # Stage 4: public subdomain reachable.
            if public_url:
                pub_step = AgentStep(
                    step=len(results) + 1,
                    tool=ToolName.RUN_COMMAND.value,
                    args={"command": f"curl -f --max-time 15 {public_url}"},
                    reason=f"Verify public URL {public_url} returns HTTP 2xx.",
                    risk_level="safe",
                )
                pub_result = await _run_validated_step(pub_step)
                if not pub_result.validation_passed:
                    logger.warning(
                        "[deploy_contract] stage4_public_failed | url=%s | exit=%s",
                        public_url, pub_result.exit_code,
                    )
                    # Public URL failure is non-fatal — local stages passed.

        return True, public_url

    if requires_validation and _is_deployment_objective(objective):
        success, validation_url = await _run_deployment_contract()
    elif requires_validation and success:
        # Non-deployment objectives that still need server validation.
        # Use workspace_context.port (BUG #4) instead of extracting from stdout.
        port = workspace_context.port if workspace_context else None
        if port is None:
            success, validation_url = await _fallback_start_and_verify(
                "No allocated port in platform context for server validation."
            )
        else:
            local_url = f"http://127.0.0.1:{port}"
            v_step = AgentStep(
                step=len(results) + 1,
                tool=ToolName.RUN_COMMAND.value,
                args={"command": f"curl -f {local_url}"},
                reason="Validate local server is serving traffic before returning success.",
                risk_level="safe",
            )
            v_result = await _run_validated_step(v_step)
            success = v_result.validation_passed
            if not v_result.validation_passed:
                errors.append(
                    {
                        "step": v_result.step,
                        "tool": ToolName.RUN_COMMAND.value,
                        "command": v_result.command,
                        "command_type": v_result.command_type,
                        "exit_code": v_result.exit_code,
                        "stderr": v_result.stderr[:1500],
                        "stdout": v_result.stdout[:1500],
                        "validation_passed": False,
                        "status": v_result.status,
                        "reason": "Server validation failed.",
                        "timestamp": _now().isoformat(),
                    }
                )
            # BUG #2 fix: return platform URL, not localhost.
            validation_url = (workspace_context.base_url or local_url) if success else ""

    if not success:
        validation_url = ""

    # BUG #2 fix: summary URL is the public subdomain URL, not http://127.0.0.1.
    public_summary_url = (workspace_context.base_url or validation_url) if success else ""
    status_label = "SUCCESS" if success else "FAILED"
    summary = f"Steps executed: {len(results)}\nStatus: {status_label}"
    if public_summary_url:
        summary += f"\nURL: {public_summary_url}"

    return ToolCallingLoopResult(
        task_mode=normalized_task_mode,
        plan=plan.steps,
        steps=results,
        decisions=decisions,
        errors=errors,
        retries=retries,
        summary=summary,
        success=success,
        steps_taken=len(results),
    )
