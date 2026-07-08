"""Clarification and user reply models — Sprint 3."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# ClarificationQuestion
# ---------------------------------------------------------------------------

class QuestionPriority(str, Enum):
    """Priority of a clarification question."""

    CRITICAL = "critical"    # blocks planning
    HIGH = "high"            # important but not blocking
    MEDIUM = "medium"
    LOW = "low"
    INFO = "info"            # nice to have


class QuestionType(str, Enum):
    """Type of clarification question."""

    REQUIREMENT = "requirement"
    ASSUMPTION = "assumption"
    ARCHITECTURE = "architecture"
    SPECIFICATION = "specification"
    CONFLICT = "conflict"
    DEPLOYMENT = "deployment"


class ClarificationQuestion(BaseModel):
    """A single deterministic clarification question.

    Produced by:
      - ``QuestionPlanner``
      - ``RequirementReview``
      - ``SpecificationReview``
      - ``ArchitectureValidator``
      - ``AssumptionEngine``
    """

    question_id: str = Field(default_factory=lambda: __import__("uuid").uuid4().hex[:12])
    question_type: QuestionType = QuestionType.REQUIREMENT
    priority: QuestionPriority = QuestionPriority.CRITICAL

    # Question content
    question: str = ""
    reason: str = ""
    required_field: str = ""  # which field this clarifies

    # Behavior
    blocking: bool = True  # if True, pause execution
    default_value: Any = None
    validation_rule: str = ""  # regex or constraint

    # Multiple choice options (empty = free text)
    options: list[str] = Field(default_factory=list)

    # Context
    source: str = ""  # which engine produced this
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


# ---------------------------------------------------------------------------
# StructuredUserReply
# ---------------------------------------------------------------------------

class ReplyType(str, Enum):
    """Deterministic structured reply types."""

    YES = "yes"
    NO = "no"
    APPROVE = "approve"
    REJECT = "reject"
    SKIP = "skip"
    USE_DEFAULT = "use_default"
    CUSTOM_VALUE = "custom_value"
    CONTINUE = "continue"
    ABORT = "abort"


class StructuredUserReply(BaseModel):
    """Parsed structured user reply."""

    reply_id: str = Field(default_factory=lambda: __import__("uuid").uuid4().hex[:12])
    reply_type: ReplyType = ReplyType.CONTINUE
    raw_message: str = ""  # original user message

    # For CUSTOM_VALUE
    custom_value: Any = None

    # For APPROVE / REJECT with specific items
    approved_items: list[str] = Field(default_factory=list)
    rejected_items: list[str] = Field(default_factory=list)
    reason: str = ""

    # Metadata
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    user: str = ""


# ---------------------------------------------------------------------------
# ClarificationSession
# ---------------------------------------------------------------------------

class ClarificationSession(BaseModel):
    """A clarification session for a job.

    Stored as JSONB in ``jobs.clarification_session``.
    """

    session_id: str = Field(default_factory=lambda: __import__("uuid").uuid4().hex[:16])
    job_id: str = ""
    conversation_id: str = ""

    questions: list[ClarificationQuestion] = Field(default_factory=list)
    answers: list[StructuredUserReply] = Field(default_factory=list)

    # State
    is_active: bool = False
    current_question_index: int = 0
    all_answered: bool = False

    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    def add_question(self, question: ClarificationQuestion) -> None:
        self.questions.append(question)
        self.is_active = True
        self.updated_at = datetime.now(timezone.utc)

    def add_answer(self, answer: StructuredUserReply) -> None:
        self.answers.append(answer)
        self.current_question_index += 1
        if self.current_question_index >= len(self.questions):
            self.all_answered = True
            self.is_active = False
        self.updated_at = datetime.now(timezone.utc)

    def get_current_question(self) -> ClarificationQuestion | None:
        if self.current_question_index < len(self.questions):
            return self.questions[self.current_question_index]
        return None
