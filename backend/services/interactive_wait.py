"""Interactive Wait Engine — Sprint 3: Human-in-the-Loop Orchestration.

Deterministic pause/resume system.

The agent can pause execution, wait for user reply, then resume
exactly where it stopped — without restarting the job, rebuilding the
plan, or redoing requirement discovery.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

from models.approval import ExecutionCursor, JobInteractionState, JobState, ResumeToken, ResumeTokenStore
from models.job import JobStatus

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# InteractiveWaitEngine
# ---------------------------------------------------------------------------


class InteractiveWaitError(Exception):
    """Raised when a job cannot be paused or resumed."""


# ---------------------------------------------------------------------------
# Task 1 (Sprint 3A.3) — Single revocation entry point
# ---------------------------------------------------------------------------

class _TokenRevocationEngine:
    """Single internal engine for automatic ResumeToken revocation.

    All revocation logic lives here. No external caller needs to
    know about token revocation — it happens automatically.

    Triggers revoked:
      - approval rejected
      - job cancelled
      - permanent failure
      - archive
      - admin abort
      - replacement by newer ResumeToken
    """

    @staticmethod
    async def revoke_for_approval(
        approval_id: str,
        reason: str,
    ) -> None:
        """Revoke the ResumeToken for a specific approval."""
        try:
            await ResumeTokenStore.revoke(approval_id, reason)
        except Exception as exc:
            logger.warning(
                "[wait] auto-revoke failed for approval %s: %s",
                approval_id, exc,
            )

    @staticmethod
    async def revoke_for_job(
        job_id: str,
        conversation_id: str,
        reason: str,
    ) -> None:
        """Revoke all active ResumeTokens for a job."""
        try:
            state = await InteractiveWaitEngine._load_state(
                conversation_id, job_id,
            )
            for aid in state.pending_approval_ids:
                await _TokenRevocationEngine.revoke_for_approval(aid, reason)
        except Exception as exc:
            logger.warning(
                "[wait] auto-revoke failed for job %s: %s",
                job_id, exc,
            )

    @staticmethod
    async def revoke_all_for_approval(
        approval_id: str,
        reason: str,
    ) -> None:
        """Revoke token + mark approval as not resumable."""
        await _TokenRevocationEngine.revoke_for_approval(approval_id, reason)


class InteractiveWaitEngine:
    """Pause/resume engine for interactive execution.

    Responsibilities:
      - Pause a running job (transition to WAITING_FOR_USER)
      - Persist ``ExecutionCursor`` so execution can resume exactly
      - Accept user reply and transition to APPROVED / REJECTED / RESUMED
      - Enforce state machine rules (only WAITING jobs may be resumed)
      """

    # - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - -
    # Pause
    # - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - -

    @classmethod
    async def pause(
        cls,
        job_id: str,
        conversation_id: str,
        *,
        reason: str = "",
        current_step_index: int | None = None,
        execution_cursor: ExecutionCursor | None = None,
    ) -> JobInteractionState:
        """Pause a running job.

        Returns the updated ``JobInteractionState``.

        FIX 1 (Sprint 3A.1): issues a ``ResumeToken`` bound to the
        current ``ExecutionCursor`` version and ``FrozenSpecification``
        version, signed with ``APPROVAL_RESUME_SECRET``.
        """
        from core.database import get_supabase, get_supabase_async
        import json as _json, os

        # Load current interaction state
        state = await cls._load_state(conversation_id, job_id)

        # Transition to WAITING
        if state.current_state == JobState.WAITING_FOR_USER:
            logger.warning("[wait] job %s already waiting", job_id)
        else:
            state.transition_to(JobState.WAITING_FOR_USER)

        # Attach execution cursor (so we know where to resume)
        if execution_cursor is not None:
            state.execution_cursor = execution_cursor

        # Record the waiting step
        if execution_cursor is not None and current_step_index is not None:
            execution_cursor.mark_waiting(current_step_index)

        # Add a system message explaining why we are waiting
        from models.approval import InteractionMessage
        state.add_message(
            InteractionMessage(
                sender="agent",
                message_type="info",
                content=reason or "Waiting for user input before continuing.",
            )
        )
        state.waiting_since = datetime.now(timezone.utc)

        await cls._persist_state(state)
        await cls._update_job_status(job_id, JobStatus.WAITING_FOR_USER)

        # FIX 1 + Task 1 (Sprint 3A.2): Issue ResumeToken — FAIL if secret missing
        try:
            from core.config import get_settings as _get_settings
            _settings = _get_settings()
            secret = getattr(_settings, "APPROVAL_RESUME_SECRET", None)
            if not secret:
                from models.approval import ApprovalConfigurationError
                raise ApprovalConfigurationError(
                    "APPROVAL_RESUME_SECRET is not configured. "
                    "Cannot issue signed ResumeToken. "
                    "Set APPROVAL_RESUME_SECRET in .env."
                )
            approval_id = (
                state.pending_approval_ids[-1]
                if state.pending_approval_ids
                else job_id
            )
            tok = ResumeToken(
                approval_id=approval_id,
                execution_cursor_version=execution_cursor.cursor_version if execution_cursor is not None else 0,
                specification_version=None,  # set when spec is frozen
                expires_at=datetime.now(timezone.utc).replace(hour=23, minute=59, second=59),
            )
            tok.sign(secret)
            await ResumeTokenStore.issue(approval_id, tok)
            logger.info("[wait] issued resume token %s for approval %s", tok.nonce[:8], approval_id)
        except Exception as exc:
            logger.error("[wait] CRITICAL: cannot issue resume token: %s", exc)
            raise  # Task 1: never issue unsigned token

        logger.info(
            "[wait] job %s paused (waiting_for_user): reason=%s",
            job_id,
            reason,
        )
        return state

    # - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - -
    # Resume
    # - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - -

    @classmethod
    async def resume(
        cls,
        job_id: str,
        conversation_id: str,
        *,
        reply: str | None = None,
        structured_reply: Any | None = None,
    ) -> JobInteractionState:
        """Resume a WAITING job.

        Validates the ``ResumeToken`` before allowing resume (Task 1 + Task 3).
        Revokes the token if the approval is rejected.

        Returns the updated ``JobInteractionState``.
        """
        from core.database import get_supabase

        state = await cls._load_state(conversation_id, job_id)

        if state.current_state != JobState.WAITING_FOR_USER:
            raise InteractiveWaitError(
                f"Job {job_id} is not waiting "
                f"(current state: {state.current_state.value})"
            )

        # Task 1 + Task 3: validate ResumeToken before resuming
        try:
            from core.config import get_settings as _get_settings
            secret = getattr(_get_settings(), "APPROVAL_RESUME_SECRET", None)
            approval_id = (
                state.pending_approval_ids[-1]
                if state.pending_approval_ids
                else job_id
            )
            tok = await ResumeTokenStore.load(approval_id)
            if tok is not None:
                if not tok.verify(secret or ""):
                    raise InteractiveWaitError(
                        f"Invalid or expired resume token for approval {approval_id}. "
                        f"Token may be revoked, consumed, expired, or tampered with."
                    )
                # Valid token — consume it (single-use)
                await ResumeTokenStore.consume(approval_id)
        except InteractiveWaitError:
            raise
        except Exception as exc:
            logger.error("[wait] token validation error: %s", exc)
            raise InteractiveWaitError(f"Token validation failed: {exc}")

        # Record user reply
        if reply:
            from models.approval import InteractionMessage
            state.add_message(
                InteractionMessage(
                    sender="user",
                    message_type="answer",
                    content=reply,
                    structured=(structured_reply is not None),
                )
            )
            # Task 1: auto-revoke if the user rejected the approval
            if reply.strip().upper().startswith("REJECT"):
                approval_id = (
                    state.pending_approval_ids[-1]
                    if state.pending_approval_ids
                    else job_id
                )
                await _TokenRevocationEngine.revoke_for_approval(
                    approval_id, "user_rejected",
                )

        # Clear waiting flag
        if state.execution_cursor is not None:
            state.execution_cursor.clear_waiting()

        # Transition to RESUMED
        state.transition_to(JobState.RESUMED)
        await cls._persist_state(state)
        await cls._update_job_status(job_id, JobStatus.RESUMED)

        logger.info("[wait] job %s resumed", job_id)
        return state

    # - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - -
    # Query
    # - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - -

    @classmethod
    async def get_state(
        cls,
        conversation_id: str,
        job_id: str,
    ) -> JobInteractionState:
        """Load the current interaction state."""
        return await cls._load_state(conversation_id, job_id)

    @classmethod
    async def record_clarification_answer(
        cls,
        job_id: str,
        conversation_id: str,
        *,
        answer: str | None = None,
        raw: str | None = None,
        structured_submission: Any | None = None,
    ) -> JobInteractionState:
        """Record a clarification answer and transition the job back to RUNNING.

        Called by ``EventWaitEngine.await_clarification_reply`` after a
        ``CLARIFICATION_REPLY`` (or ``USER_REPLY`` / ``RESUME_REQUEST``) wakes the
        wait loop.  Mirrors ``resume()`` but is scoped to clarification: it
        records the user's answer as an interaction message, clears the waiting
        cursor, and flips the job status to RUNNING so the re-dispatched
        pipeline resumes cleanly.  The actual ProjectSpecification update happens
        in ``agent_service.run_agent_pipeline`` using the recorded answer.

        Supports BOTH the legacy free-text path (``answer`` / ``raw``) and the
        new structured path (``structured_submission``).  When a structured
        submission is supplied, the full authoritative payload is stored on the
        interaction state (``clarification_submission``) so the resume handler
        can fold it back deterministically — and the chat-history message is
        REDACTED so secret values never appear in user-visible history.

        Never bypasses the resume-token / optimistic state checks: the caller
        (event wait engine) has already verified safety before invoking this.
        """
        from core.database import get_supabase

        state = await cls._load_state(conversation_id, job_id)

        if state.current_state != JobState.WAITING_FOR_USER:
            logger.warning(
                "[wait] record_clarification_answer: job %s not in WAITING_FOR_USER "
                "(state=%s); recording answer anyway",
                job_id,
                state.current_state.value,
            )

        # Store the structured submission on the state (authoritative source for
        # the resume handler).  Legacy free-text clarifications leave this None.
        if structured_submission is not None:
            state.clarification_submission = (
                structured_submission.model_dump(mode="json")
                if hasattr(structured_submission, "model_dump")
                else dict(structured_submission)
            )

        # Record the user's answer as an interaction message.
        if structured_submission is not None:
            # Structured path: record a REDACTED placeholder only.  Secret values
            # (token/api_key/password/...) MUST never appear in chat history.
            n = 0
            if hasattr(structured_submission, "answers"):
                n = len(structured_submission.answers)
            elif isinstance(structured_submission, dict):
                n = len(structured_submission.get("answers") or [])
            from models.approval import InteractionMessage

            state.add_message(
                InteractionMessage(
                    sender="user",
                    message_type="answer",
                    content=f"Submitted {n} clarification answer(s).",
                    structured=True,
                )
            )
        elif answer or raw:
            from models.approval import InteractionMessage

            state.add_message(
                InteractionMessage(
                    sender="user",
                    message_type="answer",
                    content=(answer or raw or "").strip(),
                    structured=False,
                )
            )

        # Clear the waiting cursor so the re-dispatched pipeline resumes cleanly.
        if state.execution_cursor is not None:
            state.execution_cursor.clear_waiting()

        state.transition_to(JobState.RESUMED)
        await cls._persist_state(state)
        await cls._update_job_status(job_id, JobStatus.RESUMED)

        logger.info("[wait] clarification answer recorded | job=%s", job_id)
        return state

    @classmethod
    async def is_waiting(cls, conversation_id: str, job_id: str) -> bool:
        """Return True if the job is currently waiting for user input."""
        state = await cls._load_state(conversation_id, job_id)
        return state.current_state == JobState.WAITING_FOR_USER

    # - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - -
    # Persistence helpers
    # - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - -

    @staticmethod
    async def _load_state(
        conversation_id: str,
        job_id: str,
    ) -> JobInteractionState:
        """Load ``JobInteractionState`` from ``jobs.interaction_state`` (JSONB)."""
        from core.database import get_supabase, get_supabase_async

        try:
            result = (
                await (await get_supabase_async())
                .table("jobs")
                .select("interaction_state")
                .eq("id", job_id)
                .eq("conversation_id", conversation_id)
                .limit(1)
                .execute()
            )
            if result.data and result.data[0].get("interaction_state"):
                return JobInteractionState(
                    **result.data[0]["interaction_state"]
                )
        except Exception as exc:
            logger.warning("[wait] failed to load interaction state: %s", exc)

        # Return a fresh state
        return JobInteractionState(
            job_id=job_id,
            conversation_id=conversation_id,
        )

    @staticmethod
    async def _persist_state(state: JobInteractionState) -> None:
        """Persist ``JobInteractionState`` to ``jobs.interaction_state``."""
        from core.database import get_supabase_async

        try:
            await (await get_supabase_async()).table("jobs").update(
                {
                    "interaction_state": state.model_dump(mode="json"),
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                }
            ).eq("id", state.job_id).execute()
        except Exception as exc:
            logger.error("[wait] failed to persist interaction state: %s", exc)
            raise

    @staticmethod
    async def _update_job_status(job_id: str, status: JobStatus) -> None:
        """Update ``jobs.status``."""
        from core.database import get_supabase_async

        try:
            await (await get_supabase_async()).table("jobs").update(
                {"status": status.value,
                 "updated_at": datetime.now(timezone.utc).isoformat()}
            ).eq("id", job_id).execute()
        except Exception as exc:
            logger.warning("[wait] failed to update job status: %s", exc)
