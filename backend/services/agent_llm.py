"""LLM integration for Forge v2 agent.

All public functions are fully async and return validated Pydantic models.
Redis caching is applied to plan generation keyed on (objective, context hash).
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Any

try:
    import redis.asyncio as aioredis  # type: ignore[import-untyped]
    _REDIS_AVAILABLE = True
except ImportError:
    aioredis = None  # type: ignore[assignment]
    _REDIS_AVAILABLE = False

from fastapi import HTTPException, status
from openai import AsyncOpenAI

from core.config import get_settings
from models.agent import (
    AgentDecision,
    AgentPlan,
    AgentStep,
    DecisionAction,
    StepResult,
    ToolName,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

_PLAN_SCHEMA = {
    "type": "object",
    "required": ["objective", "steps", "context_summary"],
    "additionalProperties": False,
    "properties": {
        "objective": {"type": "string"},
        "context_summary": {"type": "string"},
        "steps": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["step", "tool", "args", "rationale"],
                "additionalProperties": False,
                "properties": {
                    "step": {"type": "integer"},
                    "tool": {
                        "type": "string",
                        "enum": [t.value for t in ToolName],
                    },
                    "args": {"type": "object"},
                    "rationale": {"type": "string"},
                },
            },
        },
    },
}

_DECISION_SCHEMA = {
    "type": "object",
    "required": ["action", "reason", "summary_so_far"],
    "additionalProperties": False,
    "properties": {
        "action": {
            "type": "string",
            "enum": [a.value for a in DecisionAction],
        },
        "reason": {"type": "string"},
        "summary_so_far": {"type": "string"},
        "modified_step": {
            "anyOf": [
                {
                    "type": "object",
                    "required": ["step", "tool", "args", "rationale"],
                    "additionalProperties": False,
                    "properties": {
                        "step": {"type": "integer"},
                        "tool": {"type": "string", "enum": [t.value for t in ToolName]},
                        "args": {"type": "object"},
                        "rationale": {"type": "string"},
                    },
                },
                {"type": "null"},
            ]
        },
    },
}


def _get_openai_client() -> AsyncOpenAI:
    settings = get_settings()
    if not settings.OPENAI_API_KEY:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="OPENAI_API_KEY is not configured",
        )
    return AsyncOpenAI(api_key=settings.OPENAI_API_KEY)


def _context_hash(objective: str, context: dict[str, Any]) -> str:
    raw = json.dumps({"objective": objective, "context": context}, sort_keys=True)
    return hashlib.sha256(raw.encode()).hexdigest()


async def _get_redis_client() -> "aioredis.Redis | None":  # type: ignore[name-defined]
    """Return an async Redis client if REDIS_URL is configured, else None."""
    if not _REDIS_AVAILABLE or aioredis is None:
        logger.warning("redis package not installed; LLM caching disabled")
        return None
    settings = get_settings()
    if not settings.REDIS_URL:
        return None
    try:
        return aioredis.from_url(settings.REDIS_URL, decode_responses=True)
    except Exception as exc:
        logger.warning("Failed to connect to Redis; LLM caching disabled: %s", exc)
        return None


async def _cache_get(key: str) -> str | None:
    redis = await _get_redis_client()
    if redis is None:
        return None
    try:
        return await redis.get(key)
    except Exception as exc:
        logger.warning("Redis GET failed: %s", exc)
        return None
    finally:
        try:
            await redis.aclose()
        except Exception:
            pass


async def _cache_set(key: str, value: str, ttl_seconds: int = 300) -> None:
    redis = await _get_redis_client()
    if redis is None:
        return
    try:
        await redis.setex(key, ttl_seconds, value)
    except Exception as exc:
        logger.warning("Redis SET failed: %s", exc)
    finally:
        try:
            await redis.aclose()
        except Exception:
            pass


async def _chat_json(
    messages: list[dict[str, str]],
    schema: dict[str, Any],
    cache_key: str | None = None,
) -> dict[str, Any]:
    """Call OpenAI chat completions with JSON mode; optionally cache result."""
    if cache_key:
        cached = await _cache_get(cache_key)
        if cached:
            try:
                return json.loads(cached)
            except json.JSONDecodeError:
                pass

    settings = get_settings()
    client = _get_openai_client()

    try:
        response = await client.chat.completions.create(
            model=settings.OPENAI_MODEL,
            messages=messages,  # type: ignore[arg-type]
            response_format={"type": "json_object"},
            temperature=0.2,
        )
    except Exception as exc:
        logger.error("OpenAI call failed: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"LLM request failed: {exc}",
        )

    raw = (response.choices[0].message.content or "").strip()
    if not raw:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Empty response from LLM",
        )

    try:
        result: dict[str, Any] = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"LLM returned invalid JSON: {exc}",
        )

    if cache_key:
        await _cache_set(cache_key, raw)

    return result


# ---------------------------------------------------------------------------
# Public async functions
# ---------------------------------------------------------------------------

_PLAN_SYSTEM = """\
You are Forge v2, a production-grade DevOps AI agent.

Given an objective and server context you must produce an execution plan as JSON.

RULES:
- Use ONLY these tools: run_command, check_disk, check_memory, restart_service, read_logs, deploy_app
- run_command args: {"command": "<string>"} — only safe read-only commands unless allow_write is true
- check_disk args: {} (no args needed)
- check_memory args: {} (no args needed)
- restart_service args: {"service_name": "<systemd unit>"}
- read_logs args: {"service_name": "<systemd unit or file path>", "lines": <int, default 100>}
- deploy_app args: {"app_name": "<name>", "deploy_command": "<safe deploy command>"}
- Steps must be numbered sequentially starting at 1
- Produce at most max_steps steps
- Prefer built-in tools over run_command where possible
- Never include destructive commands (rm -rf, mkfs, dd, shutdown, reboot, passwd)

Return ONLY valid JSON matching this exact schema (no markdown, no extra keys):
{"objective": "...", "context_summary": "...", "steps": [{"step": 1, "tool": "...", "args": {}, "rationale": "..."}, ...]}
"""

_EVALUATE_SYSTEM = """\
You are Forge v2 evaluator. Given a step result decide what to do next.

Possible actions:
- "continue" — step succeeded; proceed to next step
- "retry"    — step failed temporarily; retry same step (respect max_retries)
- "modify"   — step failed; provide an alternative step via modified_step
- "abort"    — unrecoverable failure; stop the run

Return ONLY valid JSON:
{"action": "...", "reason": "...", "summary_so_far": "...", "modified_step": null_or_step_object}

modified_step schema (if action is "modify"):
{"step": <same step number>, "tool": "...", "args": {}, "rationale": "..."}
"""

_REVISE_SYSTEM = """\
You are Forge v2 planner. Given the current plan and execution history revise remaining steps.

Return a full updated plan JSON:
{"objective": "...", "context_summary": "...", "steps": [{"step": N, "tool": "...", "args": {}, "rationale": "..."}, ...]}

Only include steps that still need to be executed (i.e. steps not yet completed).
"""


async def generate_plan(
    objective: str,
    context: dict[str, Any],
    max_steps: int = 8,
) -> AgentPlan:
    """Generate a multi-step execution plan using the LLM."""
    cache_key = f"forge_v2:plan:{_context_hash(objective, {**context, 'max_steps': max_steps})}"

    user_content = json.dumps(
        {
            "objective": objective,
            "max_steps": max_steps,
            "server_metadata": context.get("server_metadata", {}),
            "failure_history": context.get("failure_history", []),
            "allow_write": context.get("allow_write", False),
        },
        indent=2,
    )

    messages = [
        {"role": "system", "content": _PLAN_SYSTEM},
        {"role": "user", "content": user_content},
    ]

    raw = await _chat_json(messages, _PLAN_SCHEMA, cache_key=cache_key)

    steps_raw = raw.get("steps", [])
    steps = []
    for s in steps_raw[:max_steps]:
        try:
            steps.append(AgentStep(**s))
        except Exception as exc:
            logger.warning("Skipping malformed plan step %s: %s", s, exc)

    return AgentPlan(
        objective=raw.get("objective", objective),
        steps=steps,
        context_summary=raw.get("context_summary", ""),
    )


async def evaluate_step(
    step: AgentStep,
    result: StepResult,
    context: dict[str, Any],
) -> AgentDecision:
    """Ask the LLM to evaluate a completed step result and decide next action."""
    user_content = json.dumps(
        {
            "step": step.model_dump(),
            "result": {
                "stdout": result.stdout,
                "stderr": result.stderr,
                "exit_code": result.exit_code,
                "duration_ms": result.duration_ms,
                "success": result.success,
            },
            "objective": context.get("objective", ""),
            "previous_steps_summary": context.get("previous_steps_summary", ""),
            "retry_count": context.get("retry_count", 0),
            "max_retries": context.get("max_retries", 3),
        },
        indent=2,
    )

    messages = [
        {"role": "system", "content": _EVALUATE_SYSTEM},
        {"role": "user", "content": user_content},
    ]

    raw = await _chat_json(messages, _DECISION_SCHEMA)

    modified_step: AgentStep | None = None
    ms_raw = raw.get("modified_step")
    if ms_raw and isinstance(ms_raw, dict):
        try:
            modified_step = AgentStep(**ms_raw)
        except Exception as exc:
            logger.warning("LLM returned invalid modified_step: %s — %s", ms_raw, exc)

    action_raw = raw.get("action", "abort")
    try:
        action = DecisionAction(action_raw)
    except ValueError:
        logger.warning("Unknown decision action '%s'; defaulting to abort", action_raw)
        action = DecisionAction.ABORT

    return AgentDecision(
        action=action,
        reason=raw.get("reason", ""),
        modified_step=modified_step,
        summary_so_far=raw.get("summary_so_far", ""),
    )


async def revise_plan(
    plan: AgentPlan,
    history: list[StepResult],
    completed_step_indices: list[int],
) -> AgentPlan:
    """Ask the LLM to revise remaining steps given execution history."""
    history_payload = [
        {
            "step": r.step,
            "tool": r.tool.value,
            "exit_code": r.exit_code,
            "success": r.success,
            "stdout_snippet": r.stdout[:500],
            "stderr_snippet": r.stderr[:200],
        }
        for r in history
    ]

    remaining_steps = [s for s in plan.steps if s.step not in completed_step_indices]

    user_content = json.dumps(
        {
            "objective": plan.objective,
            "original_plan": [s.model_dump() for s in plan.steps],
            "remaining_steps": [s.model_dump() for s in remaining_steps],
            "execution_history": history_payload,
        },
        indent=2,
    )

    messages = [
        {"role": "system", "content": _REVISE_SYSTEM},
        {"role": "user", "content": user_content},
    ]

    raw = await _chat_json(messages, _PLAN_SCHEMA)

    steps_raw = raw.get("steps", [])
    steps = []
    for s in steps_raw:
        try:
            steps.append(AgentStep(**s))
        except Exception as exc:
            logger.warning("Skipping malformed revised step %s: %s", s, exc)

    return AgentPlan(
        objective=raw.get("objective", plan.objective),
        steps=steps,
        context_summary=raw.get("context_summary", plan.context_summary),
    )
