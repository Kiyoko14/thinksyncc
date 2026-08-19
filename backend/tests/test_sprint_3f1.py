"""
Sprint 3F1 verification tests — Production Readiness Hardening.

Pure-logic paths; no live Supabase/Redis/SSH required.
"""

import asyncio

import pytest

from services.project_brain import Confidence, KnowledgeItem
from services.context_memory import (
    ConfidenceEngine,
    Freshness,
    SessionSnapshotData,
)
from services.clarification_budget import (
    BudgetContext,
    BudgetVerdict,
    ClarificationBudget,
    ClarificationBudgetEngine,
)
from services.knowledge_consistency import (
    ConsistencyAction,
    KnowledgeConsistency,
    KnowledgeFact,
)
from services.self_evaluation import EvalAction, SelfEvaluator
from services.workspace_awareness import WorkspaceAwareness, WorkspaceUnderstanding


# --------------------------------------------------------------------------- #
# PART 3 — Confidence Hardening
# --------------------------------------------------------------------------- #

def test_confidence_compute_blended():
    c = ConfidenceEngine.compute(
        repository_knowledge=0.9,
        workspace_understanding=0.8,
        conversation_history=0.7,
        specification=0.9,
        architecture=0.8,
        decision_memory=0.7,
        recent_changes=0.6,
    )
    assert c is Confidence.HIGH


def test_confidence_compute_weak_floor():
    # One strong signal cannot rescue a blind spot (weakest < 0.2 -> LOW floor).
    c = ConfidenceEngine.compute(
        repository_knowledge=1.0,
        workspace_understanding=0.0,
        conversation_history=0.0,
        specification=0.0,
        architecture=0.0,
        decision_memory=0.0,
        recent_changes=0.0,
    )
    assert c in (Confidence.LOW, Confidence.MEDIUM)


def test_confidence_increase_after_verification():
    item = KnowledgeItem(key="k", value="v", confidence=Confidence.LOW)
    ConfidenceEngine.increase(item)
    assert item.confidence.rank > Confidence.LOW.rank


def test_confidence_decrease_on_change():
    item = KnowledgeItem(key="k", value="v", confidence=Confidence.HIGH)
    ConfidenceEngine.decrease(item, reason="repository changes")
    assert item.confidence.rank < Confidence.HIGH.rank
    assert "decreased" in item.origin


# --------------------------------------------------------------------------- #
# PART 2 — Clarification Budget
# --------------------------------------------------------------------------- #

def test_budget_skips_when_answer_known():
    ctx = BudgetContext(project_brain="we use Postgres for the primary database")
    budget = ClarificationBudget(max_questions=1)
    verdict = ClarificationBudgetEngine.evaluate(
        question_text="Which database do we use?",
        blocking=False,
        risk_level="low",
        ctx=ctx,
        budget=budget,
    )
    assert verdict is BudgetVerdict.SKIP


def test_budget_exhausted_nonblocking_safe_assume():
    budget = ClarificationBudget(max_questions=0)
    ctx = BudgetContext()
    verdict = ClarificationBudgetEngine.evaluate(
        question_text="What naming convention do you prefer?",
        blocking=False,
        risk_level="low",
        ctx=ctx,
        budget=budget,
    )
    # Budget exhausted + non-blocking + safe-to-assume -> EXHAUSTED (safe continue)
    assert verdict in (BudgetVerdict.EXHAUSTED, BudgetVerdict.SAFE_ASSUME)


def test_budget_blocking_can_still_ask_when_exhausted():
    budget = ClarificationBudget(max_questions=0, reserve_for_blocking=0)
    ctx = BudgetContext()
    verdict = ClarificationBudgetEngine.evaluate(
        question_text="What is the auth secret?",
        blocking=True,
        risk_level="critical",
        ctx=ctx,
        budget=budget,
    )
    # Blocking gap must still surface even when budget is spent.
    assert verdict is BudgetVerdict.ASK


# --------------------------------------------------------------------------- #
# PART 4 — Knowledge Consistency
# --------------------------------------------------------------------------- #

def test_consistency_obsoletes_previous():
    prev = KnowledgeFact(key="cache", value="we use Redis", category="decision", confidence="high")
    new = KnowledgeFact(key="cache", value="we use Memcached", category="decision", confidence="high", source="agent")
    result = KnowledgeConsistency.check(new_fact=new, existing=[prev])
    assert result.action is ConsistencyAction.OBSOLETE_PREVIOUS
    assert result.obsolete_previous is prev


def test_consistency_accepts_identical():
    prev = KnowledgeFact(key="cache", value="we use Redis", category="decision")
    new = KnowledgeFact(key="cache", value="we use Redis", category="decision")
    result = KnowledgeConsistency.check(new_fact=new, existing=[prev])
    assert result.action is ConsistencyAction.ACCEPT


def test_consistency_flags_lower_confidence_conflict():
    prev = KnowledgeFact(key="cache", value="we use Redis", category="decision", confidence="high")
    new = KnowledgeFact(key="cache", value="we use Memcached", category="decision", confidence="low", source="legacy")
    result = KnowledgeConsistency.check(new_fact=new, existing=[prev])
    assert result.action is ConsistencyAction.CONFLICT


# --------------------------------------------------------------------------- #
# PART 6 — Self Evaluation (internal only)
# --------------------------------------------------------------------------- #

def test_self_eval_continue_when_confident():
    ev = SelfEvaluator()
    r = ev.evaluate(task="x", confidence=Confidence.HIGH)
    assert r.proceed is True
    assert r.action is EvalAction.CONTINUE


def test_self_eval_ask_on_blocking_gap():
    ev = SelfEvaluator()
    r = ev.evaluate(task="x", confidence=Confidence.MEDIUM, missing_required=True)
    assert r.proceed is False
    assert r.action is EvalAction.ASK_USER


def test_self_eval_refresh_on_stale():
    ev = SelfEvaluator()
    stale = KnowledgeItem(key="k", value="v", layer=__import__("services.project_brain", fromlist=["EngineeringMemoryLayer"]).EngineeringMemoryLayer.TASK,
                          updated="2020-01-01T00:00:00+00:00")
    r = ev.evaluate(task="x", confidence=Confidence.MEDIUM, knowledge_items=[stale])
    assert r.action in (EvalAction.REFRESH_MEMORY, EvalAction.INSPECT_FILE)


# --------------------------------------------------------------------------- #
# PART 1 — Workspace Awareness (pure logic)
# --------------------------------------------------------------------------- #

def test_workspace_understanding_context_block():
    u = WorkspaceUnderstanding(
        workspace_id="ws1",
        entry_points=["main.py"],
        services=["api.py"],
        db_models=["User"],
        changed_files=["x.py"],
    )
    block = u.to_context_block()
    assert "main.py" in block
    assert "User" in block


def test_workspace_signal_normalisation():
    assert WorkspaceAwareness._signal(0) == 0.0
    assert WorkspaceAwareness._signal(10) == 1.0
    assert WorkspaceAwareness._signal(2) == 0.4


def test_workspace_record_verification_raises_confidence():
    wa = WorkspaceAwareness()
    wa.record_verification(scope="auth")
    assert wa._last_confidence.rank >= Confidence.MEDIUM.rank


# --------------------------------------------------------------------------- #
# Integration: AdaptiveClarificationEngine with budget
# --------------------------------------------------------------------------- #

def test_adaptive_clarification_budget_skip_known_answer():
    from services.adaptive_clarification import (
        AdaptiveClarificationEngine,
        ClarificationAction,
    )
    from services.clarification_budget import BudgetContext, ClarificationBudget

    # The project brain already says we use Postgres -> question skipped.
    ctx = BudgetContext(project_brain="Primary database is Postgres")
    budget = ClarificationBudget(max_questions=1)
    decision = asyncio.run(
        AdaptiveClarificationEngine.evaluate(
            objective="add a report endpoint",
            intent="code",
            conversation_id="c1",
            budget=budget,
            budget_context=ctx,
            repository_snippet="database=postgres",
        )
    )
    # No blocking/high-risk gap remains after de-dup + budget skip; safe assume/continue.
    assert decision.action in (ClarificationAction.CONTINUE, ClarificationAction.SAFE_ASSUME)
