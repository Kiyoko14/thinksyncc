"""
Conversation Policy Engine — Objective 10 (Sprint 3B).

Question priorities:
    CRITICAL
    REQUIRED
    OPTIONAL
    INFO

Policy determines:
    ask
    skip
    auto-assume
    auto-close
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from models.interaction import ClarificationQuestion, QuestionPriority, QuestionType
from models.conversation import ConversationSession, SessionState

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# PolicyDecision
# ---------------------------------------------------------------------------

class PolicyDecision(str, Enum):
    """What to do with a question."""

    ASK = "ask"
    SKIP = "skip"
    AUTO_ASSUME = "auto_assume"
    AUTO_CLOSE = "auto_close"


# ---------------------------------------------------------------------------
# ConversationPolicyEngine
# ---------------------------------------------------------------------------

class ConversationPolicyEngine:
    """Deterministic policy engine for clarification questions.

    Policies:
      - CRITICAL questions → always ASK (block execution)
      - HIGH questions → ASK (unless auto-assume enabled)
      - MEDIUM questions → ASK or SKIP (based on user preference)
      - LOW/INFO questions → SKIP or AUTO_CLOSE
      - auto-assume: use default_value if provided
      - auto-close: close session if all questions are skippable
    """

    @classmethod
    def evaluate_question(
        cls,
        question: ClarificationQuestion,
        *,
        auto_assume: bool = False,
        auto_close: bool = False,
    ) -> PolicyDecision:
        """Evaluate a single question against policy."""
        if question.priority == QuestionPriority.CRITICAL:
            return PolicyDecision.ASK

        if question.priority == QuestionPriority.HIGH:
            if auto_assume and question.default_value is not None:
                return PolicyDecision.AUTO_ASSUME
            return PolicyDecision.ASK

        if question.priority == QuestionPriority.MEDIUM:
            if auto_close:
                return PolicyDecision.SKIP
            return PolicyDecision.ASK

        # LOW / INFO
        if auto_close:
            return PolicyDecision.AUTO_CLOSE
        return PolicyDecision.SKIP

    @classmethod
    def evaluate_session(
        cls,
        session: ConversationSession,
        *,
        auto_assume: bool = False,
        auto_close: bool = False,
    ) -> list[PolicyDecision]:
        """Evaluate all questions in a session."""
        decisions = []
        for q_dict in (session.question_history or []):
            q = ClarificationQuestion(**q_dict)
            decisions.append(
                cls.evaluate_question(
                    q,
                    auto_assume=auto_assume,
                    auto_close=auto_close,
                )
            )
        return decisions

    @classmethod
    def should_auto_close(
        cls,
        session: ConversationSession,
        *,
        auto_close: bool = False,
    ) -> bool:
        """Check if all questions are skippable."""
        if not auto_close:
            return False
        decisions = cls.evaluate_session(session, auto_close=True)
        return all(d in {PolicyDecision.SKIP, PolicyDecision.AUTO_CLOSE}
                   for d in decisions)

    @classmethod
    def apply_policy(
        cls,
        session: ConversationSession,
        *,
        auto_assume: bool = False,
        auto_close: bool = False,
    ) -> ConversationSession:
        """Apply policy to a session (mutates the session in-place).

        For AUTO_ASSUME: sets answer to default_value.
        For SKIP: marks question as skipped.
        For AUTO_CLOSE: marks session as not active.
        """
        decisions = cls.evaluate_session(
            session,
            auto_assume=auto_assume,
            auto_close=auto_close,
        )

        answer_history = session.answer_history or []
        for i, (q_dict, decision) in enumerate(
            zip(session.question_history or [], decisions)
        ):
            q = ClarificationQuestion(**q_dict)
            if decision == PolicyDecision.AUTO_ASSUME:
                from models.interaction import StructuredUserReply, ReplyType
                answer = StructuredUserReply(
                    reply_type=ReplyType.USE_DEFAULT,
                    custom_value=q.default_value,
                )
                answer_history.append(answer.model_dump(mode="json"))
                logger.info(
                    "[policy] auto-assumed %s for question %s",
                    q.default_value,
                    q.question_id,
                )
            elif decision == PolicyDecision.SKIP:
                from models.interaction import StructuredUserReply, ReplyType
                answer = StructuredUserReply(
                    reply_type=ReplyType.SKIP,
                )
                answer_history.append(answer.model_dump(mode="json"))
                logger.info("[policy] skipped question %s", q.question_id)
            elif decision == PolicyDecision.AUTO_CLOSE:
                session.state = SessionState.ARCHIVED
                logger.info("[policy] auto-closed session %s", session.session_id)
                break

        session.answer_history = answer_history
        # Update state
        if all(d in {PolicyDecision.SKIP, PolicyDecision.AUTO_CLOSE}
               for d in decisions):
            session.state = SessionState.ARCHIVED
        session.updated_at = datetime.now(timezone.utc)
        return session
