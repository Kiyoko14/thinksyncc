from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from fastapi import HTTPException, status

from core.config import get_settings
from core.value_coercion import value_to_str
from models.agent import AgentDecision, AgentPlan, AgentStep, DecisionAction, StepResult, ToolCallingLoopResult, ToolName
from services import agent_llm
from services.tools import execute_tool

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


def _is_parallel_safe(step: AgentStep) -> bool:
    tool_name = value_to_str(getattr(step, "tool", None))
    if tool_name in {ToolName.CHECK_DISK.value, ToolName.CHECK_MEMORY.value, ToolName.READ_LOGS.value}:
        return True
    if tool_name == ToolName.RUN_COMMAND.value:
        return True
    return False


async def run_server_execution(
    *,
    objective: str,
    intent: str,
    task_mode: str,
    plan_steps: list[AgentStep] | None = None,
    plan_context_summary: str | None = None,
    server: dict[str, Any],
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
) -> ToolCallingLoopResult:
    """
    Autonomous server executor with self-healing:
    - build plan (simple or LLM)
    - execute steps
    - validate via LLM evaluator
    - replan remaining steps on abort (max_heal_attempts)
    """
    normalized_intent = (intent or "").strip().lower()
    if normalized_intent != "server":
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail={"code": "INTENT_NOT_SERVER", "intent": normalized_intent})

    allow_write = True
    logger.info("Execution forced: allow_write=True")

    settings = get_settings()
    exec_config = config or ExecutionConfig()
    bounded_steps = max(1, min(int(max_steps or 8), 8))
    normalized_task_mode = (task_mode or "").strip().lower()
    if normalized_task_mode not in {"simple", "complex"}:
        normalized_task_mode = "complex"

    coordinator_context = {
        "server_metadata": {"host": server.get("host"), "ssh_user": server.get("ssh_user"), "name": server.get("name")},
        "memory": (memory or [])[-10:],
        "failure_history": [],
        "allow_write": allow_write,
        "objective": objective,
        "task_mode": normalized_task_mode,
    }

    def _signature(step: AgentStep) -> str:
        tool_name = value_to_str(getattr(step, "tool", None))
        try:
            args = json.dumps(step.args or {}, sort_keys=True, ensure_ascii=False)
        except Exception:
            args = "{}"
        return f"{tool_name}:{args}"

    failed_signatures: set[str] = set()

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
            plan = await agent_llm.generate_plan(objective=objective, context=coordinator_context, max_steps=bounded_steps)
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

    async def _exec_step(step: AgentStep) -> StepResult:
        tool_name = value_to_str(getattr(step, "tool", None))
        logger.info("[executor] step start | step=%s | tool=%s | risk=%s | args=%s", step.step, tool_name, step.risk_level, step.args)
        if on_step_start:
            try:
                await on_step_start(step.step, tool_name, step.args)
            except Exception:
                pass
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
        results.append(result)
        if on_step_result:
            try:
                await on_step_result(result)
            except Exception:
                pass
        return result

    async def _evaluate(step: AgentStep, result: StepResult, attempt: int) -> AgentDecision:
        if normalized_task_mode == "simple":
            return AgentDecision(
                action=DecisionAction.CONTINUE if result.success else DecisionAction.ABORT,
                reason="Simple mode executes one minimal command.",
                summary_so_far="",
            )
        decision = await agent_llm.evaluate_step(
            step,
            result,
            {
                **coordinator_context,
                "retry_count": attempt,
                "max_retries": min(int(settings.AGENT_MAX_RETRIES or 3), 3),
                "previous_steps_summary": " ".join(d.summary_so_far for d in decisions if d.summary_so_far),
            },
        )
        return decision

    heal_attempts = 0
    step_index = 0
    while step_index < len(plan.steps):
        step = plan.steps[step_index]
        step_sig = _signature(step)

        # Parallelize small clusters of safe diagnostics (read-only tools only).
        if normalized_task_mode == "complex" and _is_parallel_safe(step) and step_index + 1 < len(plan.steps):
            cluster: list[AgentStep] = [step]
            j = step_index + 1
            while (
                j < len(plan.steps)
                and len(cluster) < exec_config.max_parallel_diagnostics
                and _is_parallel_safe(plan.steps[j])
            ):
                cluster.append(plan.steps[j])
                j += 1

            if len(cluster) > 1:
                cluster_results = await asyncio.gather(*(_exec_step(s) for s in cluster))
                for s, r in zip(cluster, cluster_results, strict=True):
                    decision = await _evaluate(s, r, 0)
                    decisions.append(decision)
                    if on_decision:
                        try:
                            await on_decision(decision)
                        except Exception:
                            pass
                    if decision.action != DecisionAction.CONTINUE:
                        # Treat any non-continue in parallel cluster as abort point.
                        errors.append(
                            {
                                "step": s.step,
                                "tool": value_to_str(getattr(s, "tool", None)),
                                "exit_code": r.exit_code,
                                "stderr": r.stderr[:1000],
                                "reason": decision.reason,
                                "timestamp": _now().isoformat(),
                            }
                        )
                        step_index = len(plan.steps)
                        break
                else:
                    step_index = j
                    continue

        attempt = 0
        while True:
            result = await _exec_step(step)
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

            # Production reliability: never repeat the same failed action twice.
            # A retry must be an alternative approach (handled via modify/self-heal replanning).
            if decision.action == DecisionAction.RETRY:
                failed_signatures.add(step_sig)
                decision = AgentDecision(
                    action=DecisionAction.ABORT,
                    reason="Retry disallowed for identical action; switching to an alternative fix.",
                    summary_so_far=decision.summary_so_far,
                    modified_step=None,
                )
                decisions[-1] = decision

            if decision.action == DecisionAction.MODIFY and decision.modified_step is not None:
                step = decision.modified_step
                plan.steps[step_index] = step
                attempt += 1
                retries.append(
                    {"step": step.step, "tool": value_to_str(getattr(step, "tool", None)), "attempt": attempt, "timestamp": _now().isoformat(), "reason": "modify"}
                )
                continue

            # ABORT or exhausted retries: attempt self-healing replanning for remaining steps.
            errors.append(
                {
                    "step": step.step,
                    "tool": value_to_str(getattr(step, "tool", None)),
                    "exit_code": result.exit_code,
                    "stderr": result.stderr[:1500],
                    "stdout": result.stdout[:1500],
                    "reason": decision.reason,
                    "signature": step_sig,
                    "timestamp": _now().isoformat(),
                }
            )
            failed_signatures.add(step_sig)
            try:
                analysis = await agent_llm.analyze_failure(step=step, result=result, context=coordinator_context)
                if isinstance(analysis, dict) and analysis:
                    errors[-1]["analysis"] = analysis
            except Exception:
                pass

            if normalized_task_mode == "complex" and heal_attempts < exec_config.max_heal_attempts:
                heal_attempts += 1
                logger.info("[executor] self-heal attempt | attempt=%s | failed_step=%s", heal_attempts, step.step)
                try:
                    completed = [r.step for r in results if r.success]
                    revised = await agent_llm.revise_plan(
                        AgentPlan(objective=plan.objective, steps=plan.steps, context_summary=plan.context_summary),
                        history=results,
                        completed_step_indices=completed,
                    )
                    revised.steps = revised.steps[:bounded_steps]
                    # Enforce: never repeat a previously failed identical action.
                    revised.steps = [s for s in revised.steps if _signature(s) not in failed_signatures]
                    # Splice: keep completed successes, then revised remaining.
                    keep = [s for s in plan.steps if s.step in completed]
                    remaining = [AgentStep(step=i + 1 + len(keep), tool=s.tool, args=s.args, reason=s.reason, risk_level=s.risk_level) for i, s in enumerate(revised.steps)]
                    plan.steps = keep + remaining
                    step_index = len(keep)
                    break
                except Exception as exc:
                    logger.warning("Self-heal replanning failed: %s", exc)

            # Give up.
            step_index = len(plan.steps)
            break

        step_index += 1

    success = bool(results) and all(r.success for r in results)
    summary = ""
    try:
        summary = await agent_llm.summarize_tool_results(objective=objective, results=results)
    except Exception:
        summary = ""

    if not summary:
        ok = sum(1 for r in results if r.success)
        summary = f"Executed {len(results)} step(s); {ok} succeeded."

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
