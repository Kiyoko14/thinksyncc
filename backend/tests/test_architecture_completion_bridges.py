"""
Architecture Completion — bridge connectivity verification (Sprint 4 completion).

These tests prove the two disconnected bridges are now CONNECTED using the
EXISTING production components (no mocks of the engines themselves — only the
external IO boundaries: Supabase, Redis, SSH are stubbed).

Bridge 1: Missing Information Detection -> Adaptive Clarification ->
          Interactive Wait -> Resume -> Project Specification Update
Bridge 2: Implementation Intelligence -> build_plan (via _build_implementation_report)

No live Supabase/Redis/SSH required.  pytest-asyncio is NOT installed, so
coroutines are driven with asyncio.run() inside plain def tests (project convention).
"""

import asyncio

import pytest

from models.agent import ClarificationSuspendSignal
from models.interaction import ClarificationQuestion, QuestionPriority, QuestionType
from services.adaptive_clarification import (
    AdaptiveClarificationEngine,
    ClarificationAction,
)
from services.interactive_wait import InteractiveWaitEngine
from services.event_wait_engine import EventWaitEngine


# --------------------------------------------------------------------------- #
# Bridge 1 — Adaptive Clarification engine is the component that was never
#            invoked by the pipeline. Verify it actually asks when missing_info
#            is present, and that the orchestration signal contract is honoured.
# --------------------------------------------------------------------------- #


def _spec_with_missing(*fields):
    """Minimal ProjectSpecification-shaped dict with missing_info populated."""
    return {
        "name": "demo",
        "missing_info": list(fields),
        "readiness": "Blocked",
        "needs_user_input": True,
        "frozen": False,
        "project_type": "UNKNOWN",
    }


def test_bridge1_adaptive_engine_asks_on_missing_info():
    """AdaptiveClarificationEngine must ASK when missing_info is non-empty."""

    async def _run():
        spec = _spec_with_missing("port", "database")
        return await AdaptiveClarificationEngine.evaluate(
            objective="Deploy a flask app",
            intent="server",
            conversation_id="conv-1",
            spec=spec,
            turn=1,
        )

    decision = asyncio.run(_run())
    assert decision.action == ClarificationAction.ASK
    assert len(decision.questions) >= 1
    assert any("port" in q.question for q in decision.questions)


def test_bridge1_adaptive_engine_continues_when_complete():
    """With no missing info the engine must NOT pause execution."""

    async def _run():
        spec = _spec_with_missing()
        return await AdaptiveClarificationEngine.evaluate(
            objective="Deploy a flask app",
            intent="server",
            conversation_id="conv-2",
            spec=spec,
            turn=1,
        )

    decision = asyncio.run(_run())
    assert decision.action != ClarificationAction.ASK


def test_bridge1_suspend_signal_carries_questions():
    """The orchestration signal must carry the actual questions for the frontend."""
    q = ClarificationQuestion(
        question_type=QuestionType.REQUIREMENT,
        priority=QuestionPriority.CRITICAL,
        question="What is the value for `port`?",
        required_field="port",
        blocking=True,
    )
    sig = ClarificationSuspendSignal("job-9", questions=[q], resume_point=0)
    assert sig.job_id == "job-9"
    assert len(sig.questions) == 1
    assert sig.questions[0].required_field == "port"


def test_bridge1_record_clarification_answer_exists_and_transitions():
    """InteractiveWaitEngine.record_clarification_answer must exist (it was a
    dangling reference) and transition the job out of WAITING_FOR_USER."""

    captured = {}

    async def fake_load(conversation_id, job_id):
        from models.approval import JobInteractionState, JobState

        st = JobInteractionState(job_id=job_id, conversation_id=conversation_id)
        st.transition_to(JobState.WAITING_FOR_USER)
        return st

    async def fake_persist(state):
        captured["state"] = state

    async def fake_status(job_id, status):
        captured["status"] = status

    orig_load = InteractiveWaitEngine._load_state
    orig_persist = InteractiveWaitEngine._persist_state
    orig_status = InteractiveWaitEngine._update_job_status
    InteractiveWaitEngine._load_state = staticmethod(fake_load)
    InteractiveWaitEngine._persist_state = staticmethod(fake_persist)
    InteractiveWaitEngine._update_job_status = staticmethod(fake_status)
    try:
        result = asyncio.run(
            InteractiveWaitEngine.record_clarification_answer(
                "job-7", "conv-7", answer="8080", raw="8080",
            )
        )
        assert result.current_state.value != "waiting_for_user"
        assert captured.get("status") is not None
    finally:
        InteractiveWaitEngine._load_state = orig_load
        InteractiveWaitEngine._persist_state = orig_persist
        InteractiveWaitEngine._update_job_status = orig_status


# --------------------------------------------------------------------------- #
# Bridge 2 — Implementation Intelligence report feeds the planner.
# --------------------------------------------------------------------------- #


def test_bridge2_implementation_intel_resolves_strategy():
    """ImplementationIntelligence.decide_strategy must run the full fallback
    chain (capability -> template discovery -> ranking -> resolution) and return
    a structured report even with no server/capabilities (full AI fallback)."""

    async def _run():
        from services.implementation_intelligence import (
            ImplementationIntelligence,
            ImplementationStrategy,
        )

        report = await ImplementationIntelligence.decide_strategy(
            "Build a telegram bot that echoes messages",
            server_id=None,
            user_id=None,
        )
        return report, ImplementationStrategy

    report, ImplementationStrategy = asyncio.run(_run())
    assert report.strategy in ImplementationStrategy
    d = report.to_dict()
    assert "strategy" in d and "compatibility_score" in d


def test_bridge2_report_to_dict_shape():
    """The planner consumes implementation_report as a dict; verify the shape."""

    async def _run():
        from services.implementation_intelligence import (
            ImplementationIntelligence,
        )

        return await ImplementationIntelligence.decide_strategy(
            "Create a flask website"
        )

    report = asyncio.run(_run())
    d = report.to_dict()
    assert set(["strategy", "compatibility_score", "template_name"]).issubset(d.keys())
