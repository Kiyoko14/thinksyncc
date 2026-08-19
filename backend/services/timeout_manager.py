"""
Timeout Manager — Objective 7 (Sprint 3B).

Conversation timeout states:
    ACTIVE
    WAITING
    EXPIRED
    ARCHIVED
    CANCELLED

Support:
    automatic expiration
    manual cancellation
    safe resume
    timeout recovery
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from models.conversation import ConversationSession, ConversationSessionStore, SessionState
from models.approval import JobState
from services.interactive_wait import InteractiveWaitEngine, InteractiveWaitError
from services.conversation_reliability import OptimisticLockGuard

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# TimeoutError
# ---------------------------------------------------------------------------

class TimeoutError(Exception):
    """Raised when a session has timed out."""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(f"Timeout: {reason}")


# ---------------------------------------------------------------------------
# TimeoutManager
# ---------------------------------------------------------------------------

class TimeoutManager:
    """Manage session timeouts.

    States:
      ACTIVE   → WAITING   → EXPIRED
         ↓            ↓
      CANCELLED   ARCHIVED

    Features:
      - automatic expiration (check on access)
      - manual cancellation
      - safe resume (checks timeout before resuming)
      - timeout recovery (archive expired sessions)
    """

    DEFAULT_TIMEOUT_SECONDS: int = 600  # 10 minutes

    # -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- --
    # Public API
    # -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- --

    @classmethod
    async def set_timeout(
        cls,
        job_id: str,
        conversation_id: str,
        *,
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    ) -> ConversationSession:
        """Set timeout for a session."""
        session = await ConversationSessionStore.load_or_create(
            job_id, conversation_id,
        )
        session.set_timeout(timeout_seconds)
        await OptimisticLockGuard.save_session_atomic(session)
        logger.info(
            "[timeout] set timeout %ss for job %s",
            timeout_seconds, job_id,
        )
        return session

    @classmethod
    async def check_timeout(
        cls,
        job_id: str,
    ) -> ConversationSession | None:
        """Check if a session has timed out. Auto-expire if needed.

        Returns the session (if active) or None (if expired/cancelled).
        """
        session = await ConversationSessionStore.load(job_id)
        if session is None:
            return None

        if session.state in {
            SessionState.CANCELLED,
            SessionState.ARCHIVED,
        }:
            return None

        if session.is_expired():
            await cls._expire_session(session)
            return None

        return session

    @classmethod
    async def cancel(
        cls,
        job_id: str,
        conversation_id: str,
    ) -> None:
        """Manually cancel a session."""
        session = await ConversationSessionStore.load(job_id)
        if session:
            session.transition_to(SessionState.CANCELLED)
            await OptimisticLockGuard.save_session_atomic(session)

            # Also update interaction state
            try:
                state = await InteractiveWaitEngine._load_state(
                    conversation_id, job_id,
                )
                state.transition_to(JobState.CANCELLED)
                await InteractiveWaitEngine._persist_state(state)
            except Exception:
                pass

            logger.info("[timeout] session cancelled for job %s", job_id)

    @classmethod
    async def safe_resume(
        cls,
        job_id: str,
        conversation_id: str,
    ) -> bool:
        """Check if it's safe to resume (session not expired/cancelled).

        Returns True if safe to resume.
        """
        session = await ConversationSessionStore.load(job_id)
        if session is None:
            return True  # no session = safe
        if session.is_expired():
            return False
        if session.state in {
            SessionState.CANCELLED,
            SessionState.ARCHIVED,
        }:
            return False
        return True

    @classmethod
    async def recover_expired(
        cls,
        conversation_id: str,
    ) -> int:
        """Archive all expired sessions for a conversation.

        Returns the number of sessions archived.
        """
        from core.database import get_supabase, get_supabase_async

        # This is a simplified version — in production you'd query
        # the conversation_audit table. For now, load sessions from
        # active jobs.
        count = 0
        try:
            result = (
                await (await get_supabase_async())
                .table("jobs")
                .select("id, conversation_session")
                .eq("conversation_id", conversation_id)
                .execute()
            )
            for row in (result.data or []):
                session_dict = row.get("conversation_session")
                if session_dict:
                    session = ConversationSession(**session_dict)
                    if session.is_expired() and session.state == SessionState.WAITING:
                        session.transition_to(SessionState.EXPIRED)
                        await OptimisticLockGuard.save_session_atomic(session)
                        count += 1
        except Exception as exc:
            logger.warning("[timeout] recovery failed: %s", exc)
        return count

    # -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- --
    # Internal helpers
    # -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- --

    @classmethod
    async def _expire_session(cls, session: ConversationSession) -> None:
        """Mark a session as expired."""
        session.transition_to(SessionState.EXPIRED)
        await OptimisticLockGuard.save_session_atomic(session)

        # Update interaction state
        try:
            state = await InteractiveWaitEngine._load_state(
                session.conversation_id, session.job_id,
            )
            state.transition_to(JobState.FAILED)
            await InteractiveWaitEngine._persist_state(state)
        except Exception:
            pass

        logger.warning(
            "[timeout] session expired for job %s",
            session.job_id,
        )
