from __future__ import annotations

import logging
import re
from typing import Any

from models.agent import AgentPlan, AgentStep, ToolName
from models.approval import FrozenSpecViolationError, ensure_frozen_spec_immutable
from services import agent_llm
from services.capability_service import detect_capabilities
from services.templates import template_execution_hint

logger = logging.getLogger(__name__)


class ApprovedPlanViolationError(Exception):
    """Raised when ``build_plan()`` detects an attempt to rebuild an approved plan."""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(f"ApprovedPlanViolation: {reason}")


_DEPLOYMENT_RE = re.compile(r"\b(deploy|server|app|run|website)\b", re.IGNORECASE)


def _spec_to_context(spec: Any | None) -> dict[str, Any]:
    """Convert a ``ProjectSpecification`` to a plain dict for the LLM context.

    Returns an empty dict (rather than ``None``) when no spec is available
    so the LLM prompt template can simply check ``if project_specification``.
    """
    if spec is None:
        return {}
    if isinstance(spec, dict):
        return spec
    # Pydantic model
    try:
        return spec.model_dump(mode="json")
    except Exception:
        return {}


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
    workspace_context: Any | None = None,
    project_spec: Any | None = None,  # ProjectSpecification | None
    implementation_report: dict[str, Any] | None = None,  # Sprint 3C.B
) -> dict[str, Any]:
    """Build a structured plan.

    If ``project_spec`` is FROZEN (approved), the plan is immutable.
    Re-building or re-ordering raises ``FrozenSpecViolationError``.

    Returns:
      {
          "task_mode": "simple"|"complex",
          "plan": [ ... ],
          "context_summary": str
      }
    """
    # Task 2 (Sprint 3A.4): use the ONE shared global guard
    ensure_frozen_spec_immutable(project_spec, context="planner")
    normalized_intent = (intent or "").strip().lower()
    normalized_task_mode = (task_mode or "").strip().lower()
    if _DEPLOYMENT_RE.search(objective or ""):
        normalized_task_mode = "complex"
    bounded_steps = max(1, min(int(max_steps or 8), 8))
    allow_write = bool(allow_write)

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

    # BUG #1 fix: inject authoritative platform context so the LLM always
    # knows the allocated port, subdomain, protocol, and runtime type.
    # workspace_context is a WorkspaceContext instance; use as_dict() if present.
    workspace_platform: dict[str, Any] = {}
    if workspace_context is not None and hasattr(workspace_context, "as_dict"):
        workspace_platform = workspace_context.as_dict()

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
        # Sprint 3C.B: use ImplementationReport if available, else fall back to old template hint
        "template": (implementation_report or {}) if implementation_report else (template_execution_hint(objective) or {"matched": False}),
        "implementation_report": implementation_report or {},
        "workspace_platform": workspace_platform,
        "project_specification": _spec_to_context(project_spec),
        # Sprint 2B: log readiness so the planner LLM can see it
        "spec_readiness": _spec_to_context(project_spec).get("readiness", "Blocked"),
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
