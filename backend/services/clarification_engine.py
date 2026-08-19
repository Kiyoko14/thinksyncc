"""Clarification Engine — Sprint 3.

Produces deterministic clarification questions from review/validation results.
Never generates random LLM questions — only translates structured review
output into ``ClarificationQuestion`` objects.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from models.interaction import (
    ClarificationQuestion,
    ClarificationSession,
    QuestionPriority,
    QuestionType,
)
from services.conversation_policy import (
    ConversationPolicyEngine,
    PolicyDecision,
)
from services.requirement_discovery import (
    ArchitectureValidator,
    AssumptionEngine,
    QuestionPlanner,
    SpecificationReview,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# ClarificationEngine
# ---------------------------------------------------------------------------


class ClarificationEngine:
    """Convert review/validation results into clarification questions.

    Question sources (deterministic):
      - ``QuestionPlanner``      → missing requirement fields
      - ``SpecificationReview``   → specification issues
      - ``ArchitectureValidator`` → architecture conflicts
      - ``AssumptionEngine``      → unapproved assumptions
      """

    # - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - -
    # Public API
    # - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - -

    @classmethod
    def build_session(
        cls,
        job_id: str,
        conversation_id: str,
        *,
        review_result: dict[str, Any] | None = None,
        architecture_issues: list[dict[str, Any]] | None = None,
        unapproved_assumptions: list[dict[str, Any]] | None = None,
        missing_fields: list[str] | None = None,
    ) -> ClarificationSession:
        """Build a ``ClarificationSession`` from review results.

        Returns a session with 0 or more questions.

        Policy decisions (ASK / SKIP / AUTO_ASSUME / AUTO_CLOSE) are delegated
        to ``ConversationPolicyEngine`` — the single source of truth for
        clarification policy. Feature flags ``auto_assume`` / ``auto_close``
        default to ``False`` so production behaviour is unchanged.
        """
        session = ClarificationSession(
            job_id=job_id,
            conversation_id=conversation_id,
        )

        # 1. Missing requirement fields → critical questions
        if missing_fields:
            for field in missing_fields:
                q = ClarificationQuestion(
                    question_type=QuestionType.REQUIREMENT,
                    priority=QuestionPriority.CRITICAL,
                    question=f"What is the value for `{field}`?",
                    reason=f"`{field}` is required but was not provided.",
                    required_field=field,
                    blocking=True,
                )
                if cls._policy_allows(q):
                    session.add_question(q)

        # 2. Specification review issues → high/medium questions
        if review_result:
            issues = review_result.get("issues", [])
            for issue in issues:
                q = ClarificationQuestion(
                    question_type=QuestionType.SPECIFICATION,
                    priority=cls._severity_to_priority(
                        issue.get("severity", "medium")
                    ),
                    question=issue.get("message", "Clarification needed."),
                    reason=issue.get("reason", ""),
                    blocking=issue.get("blocking", True),
                )
                if cls._policy_allows(q):
                    session.add_question(q)

        # 3. Architecture validation issues → high questions
        if architecture_issues:
            for issue in architecture_issues:
                q = ClarificationQuestion(
                    question_type=QuestionType.ARCHITECTURE,
                    priority=QuestionPriority.HIGH,
                    question=issue.get("message", "Architecture issue."),
                    reason=issue.get("reason", ""),
                    blocking=True,
                )
                if cls._policy_allows(q):
                    session.add_question(q)

        # 4. Unapproved assumptions → medium questions
        if unapproved_assumptions:
            for assumption in unapproved_assumptions:
                q = ClarificationQuestion(
                    question_type=QuestionType.ASSUMPTION,
                    priority=QuestionPriority.MEDIUM,
                    question=(
                        f"Assumption: {assumption.get('field', '')} = "
                        f"{assumption.get('value', '')}. OK to proceed?"
                    ),
                    reason=assumption.get("reason", ""),
                    blocking=False,
                )
                if cls._policy_allows(q):
                    session.add_question(q)

        session.is_active = len(session.questions) > 0
        return session

    @classmethod
    def _policy_allows(cls, question: ClarificationQuestion) -> bool:
        """Return True if ``ConversationPolicyEngine`` permits this question.

        Delegates the ASK / SKIP / AUTO_ASSUME / AUTO_CLOSE decision to the
        policy engine. Both flags default to False so production behaviour is
        preserved (CRITICAL / HIGH / MEDIUM → ASK, never skipped).
        """
        decision = ConversationPolicyEngine.evaluate_question(
            question,
            auto_assume=False,
            auto_close=False,
        )
        # AUTO_ASSUME / AUTO_CLOSE are not applied here (flags off); only SKIP
        # removes a question from the session.
        return decision != PolicyDecision.SKIP

    @classmethod
    async def evaluate_review(
        cls,
        review_result: dict[str, Any],
    ) -> list[ClarificationQuestion]:
        """Evaluate a ``SpecificationReview`` result and return questions.

        This is the main entry point used by ``agent_service.py``.
        """
        status = review_result.get("status", "unknown")
        if status in {"approved", "ok"}:
            return []

        issues = review_result.get("issues", [])
        questions: list[ClarificationQuestion] = []

        for issue in issues:
            questions.append(
                ClarificationQuestion(
                    question_type=QuestionType.SPECIFICATION,
                    priority=cls._severity_to_priority(
                        issue.get("severity", "medium")
                    ),
                    question=issue.get("message", "Clarification needed."),
                    reason=issue.get("reason", ""),
                    blocking=issue.get("blocking", True),
                )
            )

        return questions

    # - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - -
    # Helpers
    # - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - -

    @staticmethod
    def _severity_to_priority(severity: str) -> QuestionPriority:
        """Map review severity to question priority."""
        mapping = {
            "critical": QuestionPriority.CRITICAL,
            "high": QuestionPriority.HIGH,
            "medium": QuestionPriority.MEDIUM,
            "low": QuestionPriority.LOW,
            "info": QuestionPriority.INFO,
        }
        return mapping.get(severity.lower(), QuestionPriority.MEDIUM)
