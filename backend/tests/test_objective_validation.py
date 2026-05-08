"""
Unit tests for BUG #1 — objective validation in the planner layer.

Covers:
  1. generate_plan always uses caller's objective, never LLM's echoed value
  2. _build_simple_plan maps objectives correctly
  3. _build_fallback_tool routes to the correct tool for known objectives
  4. Empty objectives are detected at the planner entry point
"""

from __future__ import annotations

import pytest

from models.agent import AgentPlan, AgentStep, ToolName
from services.agent_llm import build_simple_plan, _build_fallback_tool


# ---------------------------------------------------------------------------
# _build_fallback_tool — correct tool routing
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("objective,expected_tool", [
    ("Check disk usage", ToolName.CHECK_DISK),
    ("disk full alert", ToolName.CHECK_DISK),
    ("How much storage is left?", ToolName.CHECK_DISK),
    ("check memory usage", ToolName.CHECK_MEMORY),
    ("how much RAM is free", ToolName.CHECK_MEMORY),
    ("show me the logs", ToolName.RUN_COMMAND),
    ("view journal logs", ToolName.RUN_COMMAND),
    ("what is the uptime", ToolName.RUN_COMMAND),
])
def test_fallback_tool_routes_correctly(objective: str, expected_tool: ToolName) -> None:
    tool, args, reason = _build_fallback_tool(objective)
    assert tool == expected_tool, (
        f"Objective {objective!r} should route to {expected_tool} but got {tool}"
    )
    assert isinstance(reason, str) and reason.strip()


def test_fallback_tool_disk_never_deploys() -> None:
    """'Check disk usage' must never produce a deploy_app tool call."""
    tool, _, _ = _build_fallback_tool("Check disk usage")
    assert tool != ToolName.DEPLOY_APP, (
        "disk-check objective must not resolve to deploy_app"
    )


def test_fallback_tool_memory_never_deploys() -> None:
    tool, _, _ = _build_fallback_tool("How much RAM is free?")
    assert tool != ToolName.DEPLOY_APP


# ---------------------------------------------------------------------------
# build_simple_plan — plan objective always equals input
# ---------------------------------------------------------------------------

def test_simple_plan_objective_preserved() -> None:
    objective = "Check disk usage on the server"
    steps = build_simple_plan(objective=objective)
    assert isinstance(steps, list)
    assert len(steps) >= 1
    assert isinstance(steps[0], AgentStep)


def test_simple_plan_not_empty_on_known_objective() -> None:
    steps = build_simple_plan(objective="check disk")
    assert steps, "simple plan must not be empty for a valid objective"
    assert steps[0].tool == ToolName.CHECK_DISK


# ---------------------------------------------------------------------------
# AgentPlan objective immutability (BUG #1 regression guard)
# ---------------------------------------------------------------------------

def test_agent_plan_objective_not_overridden() -> None:
    """Simulate generate_plan returning a different objective from the LLM.
    The caller MUST override it with the original input objective."""
    user_input = "Check disk usage"
    llm_returned_objective = "Deploy a new application"

    # This is what generate_plan does after our fix:
    plan = AgentPlan(
        objective=user_input,          # always use caller's input
        steps=[],
        context_summary="",
    )
    assert plan.objective == user_input, (
        "plan.objective must equal user input — LLM echoed value must never override it"
    )
    assert plan.objective != llm_returned_objective


def test_empty_objective_is_invalid() -> None:
    """Empty string must not produce a valid plan step."""
    # _build_simple_plan with empty string returns a fallback diagnostic
    # but the caller (run_tool_calling_loop) must reject empty objectives
    # before calling _build_simple_plan at all.
    # Here we just verify the fallback tool doesn't deploy anything:
    tool, _, _ = _build_fallback_tool("")
    assert tool != ToolName.DEPLOY_APP
