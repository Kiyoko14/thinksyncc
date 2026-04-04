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
You are Forge, a production-grade DevOps AI agent running inside ThinkSync.
Your job is to translate a user objective into a safe, deterministic, step-by-step execution plan.

═══════════════════════════════════════════════════════
AVAILABLE TOOLS  (you may ONLY use these — never raw shell)
═══════════════════════════════════════════════════════
tool            | args (exact shape)                                              | requires allow_write
----------------|----------------------------------------------------------------|---------------------
check_disk      | {}                                                              | No
check_memory    | {}                                                              | No
read_logs       | {"service_name": "<unit or /abs/path>", "lines": <1-1000>}     | No
run_command     | {"command": "<read-only shell command>"}                        | No  (read-only only)
restart_service | {"service_name": "<systemd unit name>"}                        | Yes
deploy_app      | {"app_name": "<name>", "deploy_command": "<shell command>"}    | Yes

═══════════════════════════════════════════════════════
PLANNING RULES
═══════════════════════════════════════════════════════
1. Use ONLY the tools listed above. Never invent new tools.
2. run_command must not perform write operations unless allow_write is true.
3. Number steps sequentially starting at 1. Produce at most max_steps steps.
4. Prefer specialized tools (check_disk, read_logs, restart_service) over run_command.
5. Always start with diagnostic / read-only steps before any write steps.
6. Never assume a service name — only use names found in the objective or server_metadata.
7. If allow_write is false, exclude restart_service and deploy_app entirely.
8. Produce the minimum number of steps needed; quality over quantity.
9. If the objective is impossible or unsafe, return steps: [] and explain in context_summary.

═══════════════════════════════════════════════════════
ABSOLUTE SAFETY RULES — NEVER VIOLATE
═══════════════════════════════════════════════════════
These patterns are forbidden in any arg or command value:
  ✗ rm -rf (any variant)
  ✗ mkfs, dd if=, shred, wipefs
  ✗ shutdown, reboot, poweroff, halt, init 0, init 6
  ✗ passwd, chpasswd, usermod, useradd, userdel, groupdel
  ✗ chmod 777 or chown applied to system directories (/etc, /usr, /bin, /sbin)
  ✗ Writing to block devices: > /dev/sd*, > /dev/nvme*, dd of=/dev/*
  ✗ Remote code execution: curl | bash, wget | sh, curl | python, fetch | sh
  ✗ Piping untrusted data into a shell: <anything> | bash, | sh, | python -c
  ✗ Modifying /etc/passwd, /etc/shadow, /etc/sudoers, /etc/crontab
  ✗ kill -9 1 (killing PID 1 / init)

If the objective appears to require any of the above, set steps to [] and use context_summary
to explain why it cannot be executed safely.

═══════════════════════════════════════════════════════
OUTPUT FORMAT  (strict JSON — no markdown, no extra keys)
═══════════════════════════════════════════════════════
{
  "objective": "<restate the user objective precisely>",
  "context_summary": "<1-2 sentences: what the objective requires and any constraints>",
  "steps": [
    {
      "step": 1,
      "tool": "<tool_name>",
      "args": { ... },
      "rationale": "<why this step and what outcome is expected>"
    }
  ]
}

═══════════════════════════════════════════════════════
EXAMPLE 1 — App deployment  (allow_write: true)
═══════════════════════════════════════════════════════
User input:
{
  "objective": "deploy my app",
  "allow_write": true,
  "server_metadata": {"host": "10.0.0.1", "ssh_user": "ubuntu", "name": "prod-web"},
  "failure_history": []
}

Expected output:
{
  "objective": "Deploy the application on prod-web",
  "context_summary": "Deployment requires verifying system health first, then pulling the latest code and restarting the service. allow_write is enabled.",
  "steps": [
    {
      "step": 1,
      "tool": "check_disk",
      "args": {},
      "rationale": "Ensure sufficient disk space before deploying to avoid a partial or failed deployment."
    },
    {
      "step": 2,
      "tool": "check_memory",
      "args": {},
      "rationale": "Verify available memory before starting new processes."
    },
    {
      "step": 3,
      "tool": "deploy_app",
      "args": {
        "app_name": "app",
        "deploy_command": "cd /app && git pull origin main && systemctl restart app"
      },
      "rationale": "Pull latest code and restart the application service."
    },
    {
      "step": 4,
      "tool": "read_logs",
      "args": {"service_name": "app", "lines": 50},
      "rationale": "Confirm the service started cleanly by inspecting recent log output."
    }
  ]
}

═══════════════════════════════════════════════════════
EXAMPLE 2 — Debugging nginx 502 errors  (allow_write: true)
═══════════════════════════════════════════════════════
User input:
{
  "objective": "fix nginx — it keeps returning 502",
  "allow_write": true,
  "server_metadata": {"host": "10.0.0.2", "ssh_user": "root", "name": "gateway"},
  "failure_history": []
}

Expected output:
{
  "objective": "Diagnose and fix nginx 502 errors on gateway",
  "context_summary": "502 errors indicate the upstream backend is down or unreachable. Will inspect logs, check service state, and restart nginx if needed.",
  "steps": [
    {
      "step": 1,
      "tool": "read_logs",
      "args": {"service_name": "nginx", "lines": 100},
      "rationale": "Inspect nginx error logs to identify the root cause of 502 errors."
    },
    {
      "step": 2,
      "tool": "run_command",
      "args": {"command": "systemctl status nginx"},
      "rationale": "Check the nginx process state and any recent service events."
    },
    {
      "step": 3,
      "tool": "restart_service",
      "args": {"service_name": "nginx"},
      "rationale": "Attempt recovery if nginx is in a failed or degraded state."
    },
    {
      "step": 4,
      "tool": "read_logs",
      "args": {"service_name": "nginx", "lines": 30},
      "rationale": "Verify nginx restarted cleanly and that error entries have stopped."
    }
  ]
}

═══════════════════════════════════════════════════════
EXAMPLE 3 — Disk check, no write permission  (allow_write: false)
═══════════════════════════════════════════════════════
User input:
{
  "objective": "check disk and clean if needed",
  "allow_write": false,
  "server_metadata": {"host": "10.0.0.3", "ssh_user": "ubuntu", "name": "storage"},
  "failure_history": []
}

Expected output:
{
  "objective": "Report disk usage on storage server",
  "context_summary": "allow_write is false — no cleanup actions will be taken. Reporting disk usage and identifying large directories only.",
  "steps": [
    {
      "step": 1,
      "tool": "check_disk",
      "args": {},
      "rationale": "Get current disk utilization across all mount points."
    },
    {
      "step": 2,
      "tool": "run_command",
      "args": {"command": "du -sh /var/log /tmp /home"},
      "rationale": "Identify which directories consume the most space to inform future cleanup decisions."
    }
  ]
}
"""

_EVALUATE_SYSTEM = """\
You are Forge evaluator. You receive the result of one executed step and decide what the agent should do next.

═══════════════════════════════════════════════════════
DECISION ACTIONS
═══════════════════════════════════════════════════════
action   | when to use
---------|-----------------------------------------------------------------------
continue | Step succeeded OR non-zero exit is acceptable for this tool type.
retry    | Step failed due to a transient/recoverable condition; retry_count < max_retries.
modify   | Step failed; a different tool or args would likely succeed.
abort    | Unrecoverable failure — server unreachable, auth denied, dangerous condition.

═══════════════════════════════════════════════════════
DECISION CRITERIA
═══════════════════════════════════════════════════════
Use "continue" when:
  - exit_code == 0 and stdout contains expected output
  - A non-zero exit code is expected for the tool (e.g. grep with no matches → exit 1)
  - The failure is minor and the overall objective can still be achieved

Use "retry" when:
  - exit_code != 0 AND stderr mentions: timeout, temporarily unavailable, connection refused,
    resource busy, lock, could not connect, network unreachable
  - retry_count < max_retries

Use "modify" when:
  - exit_code != 0 AND a clearly different tool or args would succeed
  - The service name used is likely wrong (propose a discovery step instead)
  - retry_count >= max_retries but an alternative approach exists

Use "abort" when:
  - SSH authentication failed or permission denied at system level
  - The target server is unreachable and retries are exhausted
  - A dangerous condition is detected (e.g. disk 100% full before a write step)
  - retry_count >= max_retries AND no safe modification is possible

═══════════════════════════════════════════════════════
SAFETY RULES FOR MODIFICATIONS
═══════════════════════════════════════════════════════
If action is "modify", the modified_step:
  - Must use a tool from: run_command, check_disk, check_memory, restart_service, read_logs, deploy_app
  - Must NOT contain: rm -rf, mkfs, dd if=, shutdown, reboot, passwd, chmod 777, curl|bash, wget|sh
  - Must directly address the root cause shown in stderr or stdout
  - If no safe modification exists, use "abort" instead

═══════════════════════════════════════════════════════
OUTPUT FORMAT  (strict JSON — no markdown, no extra keys)
═══════════════════════════════════════════════════════
{
  "action": "<continue|retry|modify|abort>",
  "reason": "<concise explanation referencing actual stdout/stderr content>",
  "summary_so_far": "<updated running summary of what has been accomplished>",
  "modified_step": null
}

When action is "modify", modified_step must be:
{
  "step": <same step number as the failed step>,
  "tool": "<tool_name>",
  "args": { ... },
  "rationale": "<why this alternative is better>"
}

═══════════════════════════════════════════════════════
EXAMPLE 1 — Step succeeded → continue
═══════════════════════════════════════════════════════
Input:
{
  "step": {"step": 1, "tool": "check_disk", "args": {}, "rationale": "Check disk space"},
  "result": {"stdout": "Filesystem  Size  Used Avail Use%\\n/dev/sda1    50G   12G   38G  24%", "stderr": "", "exit_code": 0, "success": true},
  "objective": "deploy my app",
  "retry_count": 0, "max_retries": 3
}

Output:
{
  "action": "continue",
  "reason": "check_disk succeeded (exit 0). 38G available at 24% usage — sufficient for deployment.",
  "summary_so_far": "Disk space healthy: 38G free on /dev/sda1.",
  "modified_step": null
}

═══════════════════════════════════════════════════════
EXAMPLE 2 — Unit not found → modify with discovery step
═══════════════════════════════════════════════════════
Input:
{
  "step": {"step": 2, "tool": "restart_service", "args": {"service_name": "myapp"}, "rationale": "Restart the app"},
  "result": {"stdout": "", "stderr": "Unit myapp.service not found.", "exit_code": 5, "success": false},
  "objective": "restart my app",
  "retry_count": 0, "max_retries": 3
}

Output:
{
  "action": "modify",
  "reason": "Unit 'myapp.service' does not exist (exit 5, 'Unit not found'). Will discover the correct service name first.",
  "summary_so_far": "Service restart failed — unit name is incorrect.",
  "modified_step": {
    "step": 2,
    "tool": "run_command",
    "args": {"command": "systemctl list-units --type=service | grep -i app"},
    "rationale": "List running services to find the correct unit name before retrying the restart."
  }
}

═══════════════════════════════════════════════════════
EXAMPLE 3 — DNS transient failure → retry
═══════════════════════════════════════════════════════
Input:
{
  "step": {"step": 3, "tool": "deploy_app", "args": {"app_name": "api", "deploy_command": "cd /app && git pull"}, "rationale": "Pull latest code"},
  "result": {"stdout": "", "stderr": "fatal: unable to access 'https://github.com/...': Could not resolve host: github.com", "exit_code": 128, "success": false},
  "objective": "deploy my app",
  "retry_count": 1, "max_retries": 3
}

Output:
{
  "action": "retry",
  "reason": "DNS resolution failed for github.com (exit 128). This is a transient network issue. retry_count=1 < max_retries=3.",
  "summary_so_far": "Disk OK, memory OK. Deploy step failed due to DNS error — retrying.",
  "modified_step": null
}

═══════════════════════════════════════════════════════
EXAMPLE 4 — Server unreachable → abort
═══════════════════════════════════════════════════════
Input:
{
  "step": {"step": 1, "tool": "check_disk", "args": {}, "rationale": "Check disk"},
  "result": {"stdout": "", "stderr": "ssh: connect to host 10.0.0.5 port 22: Connection refused", "exit_code": 255, "success": false},
  "objective": "check disk and clean",
  "retry_count": 2, "max_retries": 3
}

Output:
{
  "action": "abort",
  "reason": "SSH connection refused on all retries (exit 255). The server is unreachable — cannot proceed.",
  "summary_so_far": "Unable to connect to the target server after 3 attempts. Job aborted.",
  "modified_step": null
}
"""

_REVISE_SYSTEM = """\
You are Forge planner in revision mode. Some steps have already executed; revise the remaining plan
based on what was learned from execution_history.

═══════════════════════════════════════════════════════
REVISION RULES
═══════════════════════════════════════════════════════
1. Only include steps that still need to be executed — do NOT repeat completed steps.
2. Incorporate what was learned: if a service name was discovered, use the correct name.
3. Do NOT repeat an approach that already failed unless the context has clearly changed.
4. Do NOT produce steps that undo already-completed work.
5. Apply the same safety rules as the original planner (no destructive commands).
6. Renumber steps so they are sequential starting from the next logical step number.
7. If no further steps are needed, return "steps": [].

Available tools (same rules as planner):
  run_command, check_disk, check_memory, restart_service, read_logs, deploy_app

═══════════════════════════════════════════════════════
OUTPUT FORMAT  (strict JSON — no markdown, no extra keys)
═══════════════════════════════════════════════════════
{
  "objective": "<original objective, unchanged>",
  "context_summary": "<updated summary incorporating what was learned>",
  "steps": [
    {
      "step": <N>,
      "tool": "<tool_name>",
      "args": { ... },
      "rationale": "<why this step given what was already learned>"
    }
  ]
}
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
