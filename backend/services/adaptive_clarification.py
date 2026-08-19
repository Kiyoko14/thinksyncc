"""Adaptive Clarification Engine — Sprint 3C.D.

Transform the rule-based clarification flow into an *adaptive* one.

This module EXTENDS (does not replace) the existing clarification flow.  It
reuses, in order of preference:

  • ``ClarificationEngine``        — deterministic review → question translation
  • ``ImplementationIntelligence``  — skip clarification when the implementation
                                      report already carries enough information
  • ``ProjectSpecification``        — existing-specification awareness
  • ``ChatService``                 — conversation-history awareness
  • ``WorkspaceService`` + read tools — repository awareness (never ask what the
                                      repo already answers)
  • ``EventWaitEngine``             — suspend/park while waiting for an answer

Nothing here duplicates business logic that already exists elsewhere; the
engine *composes* the existing pieces and adds the adaptive decision layer
(completeness scoring, cost-aware ask-vs-assume, assumption classification,
multi-turn de-duplication).

The decision output is one of:

  CONTINUE      — requirements are complete enough; proceed with implementation
  ASK           — a genuine gap remains; ask the user (and suspend via the
                  Event Wait Engine)
  SAFE_ASSUME   — a gap exists but a safe assumption is acceptable; proceed with
                  explicit assumptions recorded

Internal scores are never exposed to the user.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from models.interaction import (
    ClarificationQuestion,
    QuestionPriority,
    QuestionType,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Decision types
# ---------------------------------------------------------------------------


class ClarificationAction(str, Enum):
    """The adaptive decision."""

    CONTINUE = "continue"
    ASK = "ask"
    SAFE_ASSUME = "safe_assume"


class AssumptionLevel(str, Enum):
    """Risk classification for safe assumptions (mirrors QuestionPriority tiers)."""

    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"

    @property
    def rank(self) -> int:
        return {
            AssumptionLevel.CRITICAL: 3,
            AssumptionLevel.HIGH: 2,
            AssumptionLevel.MEDIUM: 1,
            AssumptionLevel.LOW: 0,
        }[self]


@dataclass
class ClarificationDecision:
    """The result of an adaptive clarification evaluation."""

    action: ClarificationAction
    # Internal-only completeness score (0.0 = incomplete, 1.0 = complete).
    completeness_score: float = 0.0
    # Human-facing questions (only populated when action == ASK).
    questions: list[ClarificationQuestion] = field(default_factory=list)
    # Explicit assumptions (only populated when action == SAFE_ASSUME / ASK).
    assumptions: list[dict[str, Any]] = field(default_factory=list)
    # Why this decision was made (internal; may be logged, never shown raw).
    reasoning: str = ""
    # Internal cost estimate of asking (vs assuming). Never user-facing.
    ask_cost: float = 0.0
    assume_cost: float = 0.0
    # The turn number (multi-turn support).
    turn: int = 1

    def to_payload(self) -> dict[str, Any]:
        """Serialise for persistence / event payload (internal fields dropped)."""
        return {
            "action": self.action.value,
            "completeness_score": round(self.completeness_score, 3),
            "questions": [q.model_dump(mode="json") for q in self.questions],
            "assumptions": self.assumptions,
            "turn": self.turn,
        }


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------


class AdaptiveClarificationEngine:
    """Adaptive, context-aware clarification decision engine.

    All side-effecting reads (repo, conversation, spec) are injected by the
    caller so the engine stays testable and never reaches into global state it
    does not own.  The orchestrator (``agent_service``) performs the actual
    reads via the existing services and passes the results in.
    """

    # -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- --
    # Main entry point
    # -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- --

    @classmethod
    async def evaluate(
        cls,
        *,
        objective: str,
        intent: str = "code",
        conversation_id: str,
        # Injected context (read by the orchestrator through existing services)
        conversation_history: list[dict[str, str]] | None = None,
        existing_answers: list[str] | None = None,
        prior_questions: list[str] | None = None,
        spec: Any | None = None,  # ProjectSpecification | None
        implementation_report: Any | None = None,  # ImplementationReport | dict | None
        repository_snippet: str = "",  # concatenated relevant repo context
        review_result: dict[str, Any] | None = None,  # SpecificationReview-style
        turn: int = 1,
        max_acceptable_assumption_level: AssumptionLevel = AssumptionLevel.LOW,
        # -- Sprint 3F1, PART 2: Clarification Budget (optional, backward compatible)
        budget: Any | None = None,  # ClarificationBudget | None
        budget_context: Any | None = None,  # BudgetContext | None
    ) -> ClarificationDecision:
        """Evaluate whether to continue, ask, or safe-assume.

        Pure (no I/O beyond the deterministic ``ClarificationEngine`` review
        translation, which is awaited).  The orchestrator is responsible for
        gathering context from the existing services and passing it here.
        Returns a ``ClarificationDecision``.
        """
        conversation_history = conversation_history or []
        existing_answers = [a for a in (existing_answers or []) if a]
        prior_questions = [q for q in (prior_questions or []) if q]
        objective_l = (objective or "").strip().lower()

        # ------------------------------------------------------------------
        # 1. Requirement completeness (internal score, never user-facing)
        # ------------------------------------------------------------------
        completeness = cls._score_completeness(
            objective=objective_l,
            spec=spec,
            review_result=review_result,
        )

        # ------------------------------------------------------------------
        # 2. Implementation Intelligence integration
        #    If the implementation report / spec already answers the question,
        #    skip clarification entirely.
        # ------------------------------------------------------------------
        if cls._implementation_report_sufficient(implementation_report, spec):
            return ClarificationDecision(
                action=ClarificationAction.CONTINUE,
                completeness_score=completeness,
                reasoning="Implementation report/spec already contains enough information; skipping clarification.",
                turn=turn,
            )

        # ------------------------------------------------------------------
        # 3. Conversation awareness — never repeat a previously asked/answered
        #    question.
        # ------------------------------------------------------------------
        asked_or_answered = cls._normalize_set(
            prior_questions + existing_answers
            + [m.get("content", "") for m in conversation_history if m.get("role") == "user"]
        )

        # ------------------------------------------------------------------
        # 4. Repository awareness — if the repo already answers the gap, do not
        #    ask; record a safe assumption instead.
        # ------------------------------------------------------------------
        repo_answers = cls._repo_answers(objective_l, repository_snippet)

        # ------------------------------------------------------------------
        # 5. Build candidate questions (deterministic, reusing ClarificationEngine)
        # ------------------------------------------------------------------
        candidate_questions = await cls._candidate_questions(
            objective=objective,
            review_result=review_result,
            spec=spec,
            completeness=completeness,
        )

        # De-duplicate against conversation / prior questions / repo answers.
        filtered: list[ClarificationQuestion] = []
        assumptions: list[dict[str, Any]] = cls._derive_assumptions(spec, completeness)
        for q in candidate_questions:
            q_text = cls._normalize(q.question)
            if q_text in asked_or_answered:
                continue  # already asked or answered — multi-turn safety
            if cls._repo_already_answers(q, repo_answers):
                # Repo answers it → convert to a safe assumption, do not ask.
                cls._attach_assumption(q, level=AssumptionLevel.LOW, assumptions=assumptions)
                continue
            # Quality gate: drop low-value / cosmetic / obvious questions.
            if cls._is_low_value(q, objective_l):
                continue
            # Sprint 3F1, PART 2: Clarification Budget gate. If a budget is
            # supplied, check whether the answer already exists (then skip) or
            # whether the budget is exhausted (then only blocking/high-risk may
            # interrupt). This never duplicates logic — it reuses the budget
            # engine's pure decision.
            if budget is not None:
                from services.clarification_budget import (
                    BudgetVerdict,
                    ClarificationBudgetEngine,
                )
                verdict = ClarificationBudgetEngine.evaluate(
                    question_text=q.question,
                    blocking=bool(q.blocking),
                    risk_level=(q.priority.value if hasattr(q.priority, "value") else str(q.priority)).lower(),
                    ctx=budget_context,
                    budget=budget,
                )
                if verdict is BudgetVerdict.SKIP:
                    continue
                if verdict is BudgetVerdict.EXHAUSTED:
                    # Budget spent on non-blocking question -> safe-assume only.
                    cls._attach_assumption(q, level=AssumptionLevel.LOW, assumptions=assumptions)
                    continue
                if verdict is BudgetVerdict.SAFE_ASSUME:
                    cls._attach_assumption(q, level=AssumptionLevel.LOW, assumptions=assumptions)
                    continue
                # BudgetVerdict.ASK -> record and keep the question.
                budget.record(q.question)
            filtered.append(q)

        # ------------------------------------------------------------------
        # 6. Cost-aware decision
        # ------------------------------------------------------------------
        if not filtered:
            # Nothing genuine to ask → if there are gaps we can close with safe
            # assumptions, do so; otherwise just continue.
            if assumptions and cls._assumptions_within_risk(assumptions, max_acceptable_assumption_level):
                return ClarificationDecision(
                    action=ClarificationAction.SAFE_ASSUME,
                    completeness_score=completeness,
                    assumptions=assumptions,
                    reasoning="No genuine open questions after de-duplication; proceeding with safe assumptions.",
                    turn=turn,
                )
            return ClarificationDecision(
                action=ClarificationAction.CONTINUE,
                completeness_score=completeness,
                assumptions=assumptions,
                reasoning="No genuine open questions remain; continuing.",
                turn=turn,
            )

        # Are any of the remaining questions blocking / high-risk?
        blocking = [q for q in filtered if q.blocking]
        high_risk = [
            q for q in filtered
            if q.priority in {QuestionPriority.CRITICAL, QuestionPriority.HIGH}
        ]

        ask_cost = cls._estimate_ask_cost(filtered, turn)
        assume_cost = cls._estimate_assume_cost(filtered)

        if high_risk or blocking:
            # High implementation risk or a blocking gap → must ask (never
            # silently assume, even if an assumption exists).
            decision_action = ClarificationAction.ASK
            reasoning = (
                "High-risk/blocking gaps detected; asking clarification is "
                "required for safe implementation."
            )
        elif assumptions and cls._assumptions_within_risk(assumptions, max_acceptable_assumption_level):
            # Low/medium, non-blocking gap that a safe assumption can close
            # within acceptable risk → prefer continuing over asking (cost-aware).
            decision_action = ClarificationAction.SAFE_ASSUME
            reasoning = (
                "Low-risk, non-blocking gap can be closed by a safe assumption "
                "cheaper than asking; continuing automatically."
            )
        else:
            # Low/medium, non-blocking, but no safe assumption available → ask
            # the fewest, highest-quality questions.
            decision_action = ClarificationAction.ASK
            reasoning = (
                "Adaptive clarification: asking %d high-quality question(s) "
                "(cost-aware vs safe assumption)."
                % len(filtered)
            )

        # Tag each question with its cost estimate (internal only).
        for q in filtered:
            q.cost_estimate = round(ask_cost / max(len(filtered), 1), 3)

        return ClarificationDecision(
            action=decision_action,
            completeness_score=completeness,
            questions=filtered,
            assumptions=assumptions,
            reasoning=reasoning,
            ask_cost=ask_cost,
            assume_cost=assume_cost,
            turn=turn,
        )

    # -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- --
    # Requirement completeness
    # -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- --

    @staticmethod
    def _score_completeness(
        *,
        objective: str,
        spec: Any | None,
        review_result: dict[str, Any] | None,
    ) -> float:
        """Internal completeness score in [0, 1]. Never exposed to the user.

        Deterministic heuristics first; the spec / review result refine it.
        """
        score = 0.4  # base: an objective exists

        # Objective clarity: length + presence of a concrete verb/goal.
        words = [w for w in objective.split() if w]
        if len(words) >= 4:
            score += 0.1
        if any(k in objective for k in ("build", "create", "make", "deploy", "add", "fix", "implement")):
            score += 0.1

        # Spec present and ready.
        if spec is not None:
            readiness = getattr(spec, "readiness", None) or (
                spec.get("readiness") if isinstance(spec, dict) else None
            )
            missing = getattr(spec, "missing_info", None) or (
                spec.get("missing_info") if isinstance(spec, dict) else None
            )
            if readiness and str(readiness).lower() in {"ready", "approved", "complete"}:
                score += 0.25
            if missing:
                score -= min(0.2, 0.04 * len(missing))

        # Review issues reduce completeness.
        if review_result:
            issues = review_result.get("issues", []) or []
            blocking = [
                i for i in issues
                if i.get("blocking", False)
            ]
            score -= min(0.3, 0.06 * len(issues) + 0.1 * len(blocking))

        return max(0.0, min(1.0, score))

    # -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- --
    # Implementation Intelligence integration
    # -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- --

    @staticmethod
    def _implementation_report_sufficient(
        implementation_report: Any | None,
        spec: Any | None,
    ) -> bool:
        """True if an implementation report / spec already answers the need.

        Reuses the existing Implementation Intelligence output: if a strategy was
        decided with files/validation, there is nothing left to clarify before
        planning.
        """
        if implementation_report is None:
            return False
        # Accept either an ImplementationReport dataclass or a plain dict.
        if hasattr(implementation_report, "to_dict"):
            try:
                data = implementation_report.to_dict()
            except Exception as exc:  # noqa: BLE001 — fall back to empty dict
                logger.debug("[adaptive-clarification] to_dict failed: %s", exc)
                data = {}
        elif isinstance(implementation_report, dict):
            data = implementation_report
        else:
            data = {}

        strategy = str(data.get("strategy", "")).lower()
        files = data.get("files") or {}
        validation = data.get("validation") or {}
        if strategy and (files or validation.get("passed")):
            return True
        # Spec alone is not sufficient (we still want to confirm gaps), but a
        # fully ready spec with no missing_info is treated as sufficient.
        if spec is not None:
            missing = getattr(spec, "missing_info", None) or (
                spec.get("missing_info") if isinstance(spec, dict) else None
            )
            readiness = getattr(spec, "readiness", None) or (
                spec.get("readiness") if isinstance(spec, dict) else None
            )
            if not missing and str(readiness).lower() in {"ready", "approved", "complete"}:
                return True
        return False

    # -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- --
    # Repository awareness
    # -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- --

    @staticmethod
    def _repo_answers(objective: str, repository_snippet: str) -> set[str]:
        """Return a set of normalised keywords the repository already addresses.

        Lightweight, deterministic: extracts identifiers/keywords from the repo
        snippet so we can match a candidate question against them and avoid
        asking about something the code already defines.
        """
        if not repository_snippet:
            return set()
        text = repository_snippet.lower()
        found: set[str] = set()
        for token in objective.split():
            t = token.strip(".,;:()\"'").lower()
            if len(t) >= 3 and t in text:
                found.add(t)
        return found

    @staticmethod
    def _repo_already_answers(question: ClarificationQuestion, repo_answers: set[str]) -> bool:
        """Heuristic: does the repository already answer this question?"""
        if not repo_answers:
            return False
        q = AdaptiveClarificationEngine._normalize(question.question)
        # If the required field / key term of the question appears in repo answers.
        if question.required_field and question.required_field.lower() in repo_answers:
            return True
        for token in q.split():
            if len(token) >= 4 and token in repo_answers:
                return True
        return False

    # -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- --
    # Candidate questions (reuses ClarificationEngine)
    # -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- --

    @classmethod
    async def _candidate_questions(
        cls,
        *,
        objective: str,
        review_result: dict[str, Any] | None,
        spec: Any | None,
        completeness: float,
    ) -> list[ClarificationQuestion]:
        """Build candidate questions by composing existing engines.

        Reuses ``ClarificationEngine.evaluate_review`` for review-driven
        questions and adds spec-driven requirement gaps.  This is composition,
        not duplication.
        """
        questions: list[ClarificationQuestion] = []

        # 1. Review-driven (deterministic, reuses existing ClarificationEngine).
        if review_result:
            try:
                questions.extend(await ClarificationEngine.evaluate_review(review_result))
            except Exception as exc:
                logger.warning("[adaptive-clarification] evaluate_review failed: %s", exc)

        # 2. Spec-driven missing requirements.
        missing = cls._spec_missing(spec)
        for field_name in missing:
            questions.append(
                ClarificationQuestion(
                    question_type=QuestionType.REQUIREMENT,
                    priority=QuestionPriority.CRITICAL,
                    question=f"What is the value for `{field_name}`?",
                    reason=f"`{field_name}` is required but not specified.",
                    required_field=field_name,
                    blocking=True,
                )
            )

        # 3. Low completeness → ask about the primary goal/intent.
        if completeness < 0.55 and not questions:
            questions.append(
                ClarificationQuestion(
                    question_type=QuestionType.REQUIREMENT,
                    priority=QuestionPriority.HIGH,
                    question=(
                        "What is the primary goal of this request? A one-line "
                        "summary helps avoid wrong assumptions."
                    ),
                    reason="Objective clarity below adaptive threshold.",
                    blocking=False,
                )
            )

        return questions

    @staticmethod
    def _spec_missing(spec: Any | None) -> list[str]:
        if spec is None:
            return []
        if isinstance(spec, dict):
            missing = spec.get("missing_info") or []
        else:
            missing = getattr(spec, "missing_info", None) or []
        if isinstance(missing, list):
            return [str(m) for m in missing if m]
        return []

    # -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- --
    # Question quality + de-duplication
    # -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- --

    @staticmethod
    def _normalize(text: str) -> str:
        return " ".join((text or "").lower().split())

    @classmethod
    def _normalize_set(cls, items: list[str]) -> set[str]:
        return {cls._normalize(i) for i in items if i}

    @classmethod
    def _is_low_value(cls, question: ClarificationQuestion, objective: str) -> bool:
        """Drop cosmetic / obvious / low-value questions (brief requirement)."""
        q = cls._normalize(question.question)
        # Cosmet/style/obvious questions.
        low_value_markers = (
            "what color", "what colour", "favorite", "preferred font",
            "nice to have", "optional", "cosmetic", "spacing", "indent",
        )
        if any(m in q for m in low_value_markers):
            return True
        # If the question is essentially a restatement of the objective, skip.
        if q and q in objective:
            return True
        return False

    # -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- --
    # Assumption management
    # -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- --

    @classmethod
    def _attach_assumption(
        cls,
        question: ClarificationQuestion,
        level: AssumptionLevel,
        assumptions: list[dict[str, Any]],
    ) -> None:
        """When a question is answered by the repo, convert it to a safe assumption."""
        question.assumption_class = level.value
        assumptions.append(
            {
                "field": question.required_field or question.question,
                "value": "inferred from repository",
                "level": level.value,
                "source": "repository_awareness",
            }
        )

    @classmethod
    def _derive_assumptions(
        cls, spec: Any | None, completeness: float
    ) -> list[dict[str, Any]]:
        """Derive explicit safe assumptions from the spec / completeness.

        Only produces assumptions when they are within acceptable risk
        (see ``_assumptions_within_risk``).
        """
        assumptions: list[dict[str, Any]] = []
        if spec is not None:
            spec_assumptions = (
                spec.get("assumptions") if isinstance(spec, dict)
                else getattr(spec, "assumptions", None)
            )
            if isinstance(spec_assumptions, list):
                for a in spec_assumptions:
                    name = a.get("field") if isinstance(a, dict) else str(a)
                    level = a.get("priority") if isinstance(a, dict) else AssumptionLevel.LOW.value
                    assumptions.append(
                        {
                            "field": name,
                            "value": a.get("value", "") if isinstance(a, dict) else "",
                            "level": str(level).lower(),
                            "source": "specification",
                        }
                    )
        return assumptions

    @classmethod
    def _assumptions_within_risk(
        cls,
        assumptions: list[dict[str, Any]],
        max_level: AssumptionLevel,
    ) -> bool:
        """True if every assumption is at or below the acceptable risk level."""
        for a in assumptions:
            level = AssumptionLevel((a.get("level") or "low").lower())
            if level.rank > max_level.rank:
                return False
        return True

    # -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- --
    # Cost estimation (internal only)
    # -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- --

    @classmethod
    def _estimate_ask_cost(
        cls, questions: list[ClarificationQuestion], turn: int
    ) -> float:
        """Cost of asking: grows with #questions, priority, and turn count.

        Internal heuristic — never shown to the user.  Higher = more disruptive.
        """
        cost = 0.0
        for q in questions:
            weight = {
                QuestionPriority.CRITICAL: 1.0,
                QuestionPriority.HIGH: 0.8,
                QuestionPriority.MEDIUM: 0.5,
                QuestionPriority.LOW: 0.2,
                QuestionPriority.INFO: 0.1,
            }.get(q.priority, 0.5)
            cost += weight
        # Later turns are more costly (user already engaged once).
        cost *= 1.0 + 0.15 * max(0, turn - 1)
        return round(cost, 3)

    @classmethod
    def _estimate_assume_cost(cls, questions: list[ClarificationQuestion]) -> float:
        """Cost of making a safe assumption instead of asking.

        Higher for blocking/high-risk questions (wrong assumption is costly).
        """
        cost = 0.0
        for q in questions:
            if q.blocking:
                cost += 0.6
            if q.priority in {QuestionPriority.CRITICAL, QuestionPriority.HIGH}:
                cost += 0.5
        return round(cost, 3)


# Re-export so callers can import the deterministic builder from one place.
from services.clarification_engine import ClarificationEngine  # noqa: E402
