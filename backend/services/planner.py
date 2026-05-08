from __future__ import annotations

import logging
import re
from typing import Any

from models.agent import AgentPlan, AgentStep, ToolName
from services import agent_llm
from services.capability_service import detect_capabilities
from services.templates import template_execution_hint

logger = logging.getLogger(__name__)

_DEPLOYMENT_RE = re.compile(r"\b(deploy|server|app|run|website)\b", re.IGNORECASE)


def _default_non_server_plan(*, intent: str, objective: str) -> list[dict[str, Any]]:
    tool = "llm_generate_code" if intent == "code" else "llm_chat"
    return [{"step": 1, "tool": tool, "args": {}, "reason": "Produce the requested response safely without any server actions."}]


async def build_plan(
    *,
    intent: str,
    task_mode: str,
    objective: str,
    max_steps: int,
    allow_write: bool | None,
    server: dict[str, Any] | None = None,
    conversation_history: list[dict[str, str]] | None = None,
    memory: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """
    Build a structured plan.

    Returns:
      {
        "task_mode": "simple|complex",
        "plan": [ ... ],
        "context_summary": str
      }
    """
    normalized_intent = (intent or "").strip().lower()
    normalized_task_mode = (task_mode or "").strip().lower()
    if _DEPLOYMENT_RE.search(objective or ""):
        normalized_task_mode = "complex"
    bounded_steps = max(1, min(int(max_steps or 8), 8))
    allow_write = True
    logger.info("Execution forced: allow_write=True")

    if normalized_intent in {"chat", "code"}:
        if normalized_task_mode == "complex":
            plan = await agent_llm.generate_non_server_plan(
                intent=normalized_intent,
                objective=objective,
                max_steps=bounded_steps,
                conversation_history=conversation_history,
            )
            return {"task_mode": "complex", "plan": plan, "context_summary": "Non-server plan generated."}
        return {"task_mode": "simple", "plan": _default_non_server_plan(intent=normalized_intent, objective=objective), "context_summary": "Simple non-server request."}

    if normalized_intent != "server":
        return {"task_mode": normalized_task_mode or "simple", "plan": [], "context_summary": f"Unsupported intent: {normalized_intent!r}."}

    if server is None:
        raise ValueError("server is required when intent=='server'")

    capabilities = await detect_capabilities(server)

    if normalized_task_mode == "simple":
        steps = agent_llm.build_simple_plan(objective=objective)
        return {"task_mode": "simple", "plan": [s.model_dump(mode="json") for s in steps], "context_summary": "Single-step server plan."}

    context = {
        "server_metadata": {
            "host": server.get("host"),
            "ssh_user": server.get("ssh_user"),
            "name": server.get("name"),
        },
        "memory": (memory or [])[-10:],
        "failure_history": [],
        "allow_write": allow_write,
        "objective": objective,
        "task_mode": "complex",
        "capabilities": capabilities,
        "template": template_execution_hint(objective) or {"matched": False},
    }
    plan_result: AgentPlan = await agent_llm.generate_plan(objective=objective, context=context, max_steps=bounded_steps)
    steps: list[AgentStep] = plan_result.steps[:bounded_steps]

    # Ensure risk_level exists for every step (defensive normalization).
    normalized_steps: list[AgentStep] = []
    for idx, step in enumerate(steps, start=1):
        tool = step.tool
        risk = getattr(step, "risk_level", None) or "safe"
        if tool in {ToolName.RESTART_SERVICE, ToolName.DEPLOY_APP} and risk == "safe":
            risk = "moderate"
        normalized_steps.append(
            AgentStep(step=idx, tool=tool, args=step.args, reason=step.reason, risk_level=risk)
        )

    return {
        "task_mode": "complex",
        "plan": [s.model_dump(mode="json") for s in normalized_steps],
        "context_summary": plan_result.context_summary,
    }
