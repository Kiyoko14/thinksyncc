"""
Conversation Session Engine — Objective 1 (Sprint 3B).

ConversationSession owns the ONLY interactive state for a job.

State machine:
    ACTIVE   → WAITING   → EXPIRED
       ↓            ↓
    CANCELLED   ARCHIVED
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field
from models.approval import ApprovalConfigurationError

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# ConversationSession
# ---------------------------------------------------------------------------

class SessionState(str, Enum):
    """Lifecycle states for a ConversationSession."""

    ACTIVE = "active"
    WAITING = "waiting"    # paused for user reply
    EXPIRED = "expired"
    ARCHIVED = "archived"
    CANCELLED = "cancelled"


class ConversationSession(BaseModel):
    """Owns ALL interactive state for a single job/approval.

    This is the ONLY owner of:
      - session_id
      - conversation_id
      - job_id
      - approval_id
      - clarification_id
      - state
      - current_question
      - question_history
      - answer_history
      - patch_history
      - timeout
      - metadata
    """

    session_id: str = Field(default_factory=lambda: __import__("uuid").uuid4().hex[:16])
    conversation_id: str = ""
    job_id: str = ""
    approval_id: str = ""
    clarification_id: str = ""

    # State
    state: SessionState = SessionState.ACTIVE

    # Questions & answers (multi-turn)
    current_question: dict[str, Any] | None = None
    question_history: list[dict[str, Any]] = Field(default_factory=list)
    answer_history: list[dict[str, Any]] = Field(default_factory=list)

    # Patches applied during this session
    patch_history: list[dict[str, Any]] = Field(default_factory=list)

    # Timeout
    timeout_at: datetime | None = None  # None = no timeout

    # Reliability (Sprint 3B.1)
    session_version: int = 0  # optimistic locking (Objective 2)
    next_sequence: int = 0  # deterministic ordering (Objective 5)

    # Metadata (transport, user, etc.)
    metadata: dict[str, Any] = Field(default_factory=dict)

    # Timestamps
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    # -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- --
    # State transitions (deterministic)
    # -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- --

    def transition_to(self, new_state: SessionState) -> None:
        """Transition to ``new_state`` with validation."""
        if new_state == SessionState.WAITING:
            if self.state not in {SessionState.ACTIVE, SessionState.WAITING}:
                raise ValueError(
                    f"Cannot transition from {self.state.value} to waiting"
                )
        elif new_state == SessionState.EXPIRED:
            if self.state not in {SessionState.ACTIVE, SessionState.WAITING}:
                raise ValueError(
                    f"Cannot transition from {self.state.value} to expired"
                )
        elif new_state == SessionState.ARCHIVED:
            if self.state not in {
                SessionState.ACTIVE,
                SessionState.WAITING,
                SessionState.EXPIRED,
                SessionState.CANCELLED,
            }:
                raise ValueError(
                    f"Cannot transition from {self.state.value} to archived"
                )
        elif new_state == SessionState.CANCELLED:
            if self.state not in {SessionState.ACTIVE, SessionState.WAITING}:
                raise ValueError(
                    f"Cannot transition from {self.state.value} to cancelled"
                )
        self.state = new_state
        self.updated_at = datetime.now(timezone.utc)

    def set_timeout(self, timeout_seconds: int = 600) -> None:
        """Set session timeout (default 10 minutes)."""
        self.timeout_at = datetime.now(timezone.utc()).replace(
            second=0, microsecond=0,
        )
        self.timeout_at = datetime.fromtimestamp(
            self.timeout_at.timestamp() + timeout_seconds,
            tz=timezone.utc,
        )
        self.updated_at = datetime.now(timezone.utc())

    def is_expired(self) -> bool:
        """Check if the session has timed out."""
        if self.timeout_at is None:
            return False
        return datetime.now(timezone.utc()) > self.timeout_at

    def add_question(self, question: dict[str, Any]) -> None:
        """Record a question and set it as current."""
        self.question_history.append(question)
        self.current_question = question
        self.transition_to(SessionState.WAITING)
        self.updated_at = datetime.now(timezone.utc())

    def add_answer(self, answer: dict[str, Any]) -> None:
        """Record an answer.  Auto-transitions to ACTIVE if waiting."""
        self.answer_history.append(answer)
        self.current_question = None
        if self.state == SessionState.WAITING:
            self.transition_to(SessionState.ACTIVE)
        self.updated_at = datetime.now(timezone.utc())

    def add_patch(self, patch: dict[str, Any]) -> None:
        """Record a requirement patch applied during this session."""
        self.patch_history.append(patch)
        self.updated_at = datetime.now(timezone.utc())

    def clear(self) -> None:
        """Reset session to initial state (for restart)."""
        self.current_question = None
        self.question_history.clear()
        self.answer_history.clear()
        self.patch_history.clear()
        self.timeout_at = None
        self.state = SessionState.ACTIVE
        self.updated_at = datetime.now(timezone.utc())


# ---------------------------------------------------------------------------
# ConversationSessionStore — persist/load sessions
# ---------------------------------------------------------------------------

class ConversationSessionStore:
    """Persist ``ConversationSession`` to ``jobs.conversation_session`` (JSONB).

    NOTE: Direct calls to ``save()`` are NOT allowed.
    All writes MUST go through ``OptimisticLockGuard.save_session_atomic()``.
    ``save()`` is kept private for internal use only.
    """

    @staticmethod
    async def _save(session: ConversationSession) -> None:
        """Persist session to DB (PRIVATE — use ``OptimisticLockGuard`` instead)."""


        from core.database import get_supabase, get_supabase_async
        import json as _json

        try:
            await (await get_supabase_async()).table("jobs").update(
                {
                    "conversation_session": _json.loads(
                        session.model_dump_json()
                    ),
                    "updated_at": datetime.now(timezone.utc()).isoformat(),
                }
            ).eq("id", session.job_id).execute()
        except Exception as exc:
            logger.error("[session] failed to save session: %s", exc)
            raise

    @staticmethod
    async def load(job_id: str) -> ConversationSession | None:
        """Load session from DB."""
        from core.database import get_supabase_async

        try:
            result = (
                await (await get_supabase_async())
                .table("jobs")
                .select("conversation_session")
                .eq("id", job_id)
                .limit(1)
                .execute()
            )
            if result.data and result.data[0].get("conversation_session"):
                return ConversationSession(
                    **result.data[0]["conversation_session"]
                )
        except Exception as exc:
            logger.warning("[session] failed to load session: %s", exc)
        return None

    @staticmethod
    async def load_or_create(
        job_id: str,
        conversation_id: str,
    ) -> ConversationSession:
        """Load existing session or create a fresh one."""
        session = await ConversationSessionStore.load(job_id)
        if session is not None:
            return session
        return ConversationSession(
            job_id=job_id,
            conversation_id=conversation_id,
        )
