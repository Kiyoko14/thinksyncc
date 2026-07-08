"""
Conversation Continuation — Objective 3 (Sprint 3B).

Deterministic continuation: the system always knows exactly where execution stopped.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from models.approval import ExecutionCursor, JobInteractionState, JobState
from models.conversation import ConversationSession, SessionState

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Supported user intents (Objective 3)
# ---------------------------------------------------------------------------

class ContinuationIntent(str, Enum):
    """Deterministic user intents for conversation continuation."""

    CONTINUE = "continue"
    APPROVE = "approve"
    REJECT = "reject"
    MODIFY = "modify"
    CLARIFY = "clarify"
    CANCEL = "cancel"
    RESTART = "restart"


# ---------------------------------------------------------------------------
# ConversationContinuationEngine
# ---------------------------------------------------------------------------

class ConversationContinuationError(Exception):
    """Raised when continuation is not possible."""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(f"ContinuationError: {reason}")


class ConversationContinuationEngine:
    """Deterministic continuation engine.

    The system always knows:
      - where execution stopped
      - what question is active
      - what approval is pending
      - which specification version is active
    """

    # -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- --
    # Public API
    # -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- --

    @classmethod
    async def get_continuation_context(
        cls,
        job_id: str,
        conversation_id: str,
    ) -> dict[str, Any]:
        """Get full continuation context (Objective 3 — deterministic).

        Returns a dict with:
          - ``execution_stopped_at``: int | None (step index)
          - ``current_question``: dict | None
          - ``pending_approval_id``: str | None
          - ``spec_version``: int | None
          - ``session_state``: str | None
          - ``is_waiting``: bool
          - ``is_expired``: bool
        """
        context: dict[str, Any] = {
            "execution_stopped_at": None,
            "current_question": None,
            "pending_approval_id": None,
            "spec_version": None,
            "session_state": None,
            "is_waiting": False,
            "is_expired": False,
        }

        # 1. Load ExecutionCursor (where execution stopped)
        from services.resume_manager import ResumeManager
        try:
            bundle = await ResumeManager.load_resume_bundle(
                job_id, conversation_id,
            )
            cursor = bundle.get("execution_cursor")
            if cursor:
                context["execution_stopped_at"] = cursor.resume_point
        except Exception:
            pass

        # 2. Load InteractionState (what approval is pending)
        from services.interactive_wait import InteractiveWaitEngine
        try:
            state = await InteractiveWaitEngine.get_state(
                conversation_id, job_id,
            )
            context["is_waiting"] = (
                state.current_state == JobState.WAITING_FOR_USER
            )
            if state.pending_approval_ids:
                context["pending_approval_id"] = (
                    state.pending_approval_ids[-1]
                )
        except Exception:
            pass

        # 3. Load ConversationSession (current question, session state)
        from models.conversation import ConversationSessionStore
        try:
            session = await ConversationSessionStore.load(job_id)
            if session:
                context["current_question"] = session.current_question
                context["session_state"] = session.state.value
                context["is_expired"] = session.is_expired()
        except Exception:
            pass

        # 4. Load spec version
        from core.database import get_supabase
        try:
            result = (
                get_supabase()
                .table("jobs")
                .select("spec")
                .eq("id", job_id)
                .limit(1)
                .execute()
            )
            if result.data and result.data[0].get("spec"):
                spec = result.data[0]["spec"]
                if isinstance(spec, dict):
                    context["spec_version"] = spec.get("current_version")
        except Exception:
            pass

        return context

    @classmethod
    async def continue_conversation(
        cls,
        job_id: str,
        conversation_id: str,
        *,
        intent: ContinuationIntent,
        reply: str | None = None,
        structured_reply: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Continue the conversation based on user intent.

        Returns:
          - ``next_action``: str ("resume" | "pause" | "cancel" | "restart")
          - ``message``: str
        """
        context = await cls.get_continuation_context(
            job_id, conversation_id,
        )

        if intent == ContinuationIntent.CONTINUE:
            if not context["is_waiting"]:
                raise ConversationContinuationError(
                    "Job is not waiting for user input"
                )
            # Delegate to InteractiveWaitEngine.resume()
            from services.interactive_wait import InteractiveWaitEngine
            state = await InteractiveWaitEngine.resume(
                job_id, conversation_id,
                reply=reply,
                structured_reply=structured_reply,
            )
            return {
                "next_action": "resume",
                "message": "Execution resuming",
                "state": state.current_state.value,
            }

        if intent == ContinuationIntent.APPROVE:
            # Approve the pending approval
            approval_id = context["pending_approval_id"]
            if not approval_id:
                raise ConversationContinuationError(
                    "No pending approval to approve"
                )
            from services.approval_engine import ApprovalEngine
            engine = ApprovalEngine(job_id, conversation_id)
            request = await engine.resolve(
                approval_id,
                ApprovalDecision.APPROVED,
                reason=reply or "User approved",
                user="user",
            )
            return {
                "next_action": "resume",
                "message": "Approved. Resuming execution.",
                "approval_id": approval_id,
            }

        if intent == ContinuationIntent.REJECT:
            approval_id = context["pending_approval_id"]
            if not approval_id:
                raise ConversationContinuationError(
                    "No pending approval to reject"
                )
            from services.approval_engine import ApprovalEngine
            engine = ApprovalEngine(job_id, conversation_id)
            request = await engine.resolve(
                approval_id,
                ApprovalDecision.REJECTED,
                reason=reply or "User rejected",
                user="user",
            )
            return {
                "next_action": "cancelled",
                "message": "Rejected. Execution cancelled.",
                "approval_id": approval_id,
            }

        if intent == ContinuationIntent.CANCEL:
            return {
                "next_action": "cancel",
                "message": "Conversation cancelled.",
            }

        if intent == ContinuationIntent.RESTART:
            # Archive current session, create fresh
            from models.conversation import ConversationSessionStore
            from services.conversation_reliability import OptimisticLockGuard
            session = await ConversationSessionStore.load(job_id)
            if session:
                session.transition_to(SessionState.ARCHIVED)
                await OptimisticLockGuard.save_session_atomic(session)
            return {
                "next_action": "restart",
                "message": "Conversation restarted.",
            }

        # MODIFY / CLARIFY → delegate to RequirementPatchEngine
        return {
            "next_action": "clarify",
            "message": "Please clarify your modification.",
        }
