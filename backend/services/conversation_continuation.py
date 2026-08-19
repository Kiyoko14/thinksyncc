"""
Conversation Continuation — Objective 3 (Sprint 3B).

Deterministic continuation: the system always knows exactly where execution stopped.
"""

from __future__ import annotations
from enum import Enum

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
        from core.database import get_supabase, get_supabase_async
        try:
            result = (
                await (await get_supabase_async())
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
    def _infer_intent(
        cls,
        reply: str | None,
        structured_reply: dict[str, Any] | None,
    ) -> ContinuationIntent:
        """Infer the continuation intent from a free-text / structured reply.

        The /reply endpoint does not carry an explicit intent; the intent is
        derived from the user's words.  Anything that is not an explicit
        approve / reject / cancel / restart / modify / clarify keyword is
        treated as CONTINUE (resume with this answer) — the safe default.
        """
        text = (reply or "").strip().lower()
        if not text and not structured_reply:
            return ContinuationIntent.CONTINUE

        # Explicit keywords take precedence.
        if text.startswith("approve") or text == "approved" or text == "yes":
            return ContinuationIntent.APPROVE
        if text.startswith("reject") or text == "rejected" or text == "no":
            return ContinuationIntent.REJECT
        if text.startswith("cancel") or text == "abort":
            return ContinuationIntent.CANCEL
        if text.startswith("restart") or text.startswith("reset"):
            return ContinuationIntent.RESTART
        if text.startswith("change") or text.startswith("modify") or text.startswith("update") or "instead of" in text or "not " in text or "emas" in text:
            return ContinuationIntent.MODIFY
        if text.startswith("clarify") or text.startswith("explain") or "what do you mean" in text:
            return ContinuationIntent.CLARIFY

        # Structured replies that carry a patch are modifications.
        if structured_reply and structured_reply.get("patch"):
            return ContinuationIntent.MODIFY

        return ContinuationIntent.CONTINUE

    @classmethod
    async def continue_conversation(
        cls,
        job_id: str,
        conversation_id: str,
        *,
        intent: ContinuationIntent | None = None,
        reply: str | None = None,
        structured_reply: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Continue the conversation based on user intent.

        If ``intent`` is not supplied it is inferred from the user's ``reply``
        (the /reply endpoint does not carry an explicit intent).  The inferred
        default is CONTINUE — i.e. "resume with this answer".

        All resume paths funnel through ``EventWaitEngine.signal(...)`` so the
        single resume orchestrator (verify_resume_safety -> InteractiveWaitEngine
        .resume -> run_job re-dispatch) is always used.  This engine never calls
        ``InteractiveWaitEngine.resume()`` directly except as a defensive
        fallback when no wait is registered.

        Returns:
          - ``next_action``: str ("resume" | "pause" | "cancel" | "restart")
          - ``message``: str
        """
        if intent is None:
            intent = cls._infer_intent(reply, structured_reply)

        context = await cls.get_continuation_context(
            job_id, conversation_id,
        )

        if intent == ContinuationIntent.CONTINUE:
            if not context["is_waiting"]:
                raise ConversationContinuationError(
                    "Job is not waiting for user input"
                )
            # Delegate the wake to the EventWaitEngine orchestrator, which owns
            # the full resume chain: verify_resume_safety() -> InteractiveWaitEngine
            # .resume() (single-use ResumeToken + state transition) -> AgentService
            # .run_job() re-dispatch from the persisted cursor.  This engine must
            # NOT call InteractiveWaitEngine.resume() directly — doing so would
            # bypass the safety verification layer.
            from services.event_wait_engine import EventWaitEngine, USER_REPLY

            woke = EventWaitEngine.signal(
                job_id,
                USER_REPLY,
                conversation_id=conversation_id,
                payload={"reply": reply, "structured_reply": structured_reply},
            )
            if not woke:
                # No active wait registered (defensive): fall back to the
                # low-level resume primitive so the job is not stranded.
                from services.interactive_wait import InteractiveWaitEngine

                state = await InteractiveWaitEngine.resume(
                    job_id, conversation_id,
                    reply=reply, structured_reply=structured_reply,
                )
                return {
                    "next_action": "resume",
                    "message": "Execution resuming",
                    "state": state.current_state.value,
                }
            return {
                "next_action": "resume",
                "message": "Execution resuming",
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
            # Wake the EventWaitEngine orchestrator so it runs the full resume
            # chain (safety verify -> InteractiveWaitEngine.resume -> re-dispatch).
            from services.event_wait_engine import (
                EventWaitEngine,
                APPROVAL_RECEIVED,
            )

            EventWaitEngine.signal(
                job_id,
                APPROVAL_RECEIVED,
                conversation_id=conversation_id,
                payload={"reply": reply, "approval_id": approval_id},
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
            from services.event_wait_engine import (
                EventWaitEngine,
                APPROVAL_RECEIVED,
            )

            EventWaitEngine.signal(
                job_id,
                APPROVAL_RECEIVED,
                conversation_id=conversation_id,
                payload={"reply": reply, "approval_id": approval_id},
            )
            return {
                "next_action": "cancelled",
                "message": "Rejected. Execution cancelled.",
                "approval_id": approval_id,
            }

        if intent == ContinuationIntent.CANCEL:
            # Tear down the waiting state via the orchestrator (clears the
            # event wait + timeout task so no orphaned wait remains).
            from services.event_wait_engine import EventWaitEngine, CANCEL

            EventWaitEngine.signal(
                job_id, CANCEL, conversation_id=conversation_id,
            )
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
            # Tear down any waiting state so the restart starts clean.
            from services.event_wait_engine import EventWaitEngine, CANCEL

            EventWaitEngine.signal(
                job_id, CANCEL, conversation_id=conversation_id,
            )
            return {
                "next_action": "restart",
                "message": "Conversation restarted.",
            }

        if intent == ContinuationIntent.CLARIFY:
            # CLARIFY is an ANSWER-RECORDING flow, NOT a spec mutation.
            # Record the clarification answer through the low-level reliability
            # primitive, then wake the EventWaitEngine orchestrator with a
            # CLARIFICATION_REPLY signal.  The orchestrator runs the full
            # resume chain (verify_resume_safety -> InteractiveWaitEngine.resume
            # -> run_job re-dispatch).  RequirementPatchEngine is NOT used here.
            answer = reply
            raw = reply
            structured_submission = None
            if structured_reply and structured_reply.get("submission"):
                structured_submission = structured_reply["submission"]

            from services.interactive_wait import InteractiveWaitEngine

            await InteractiveWaitEngine.record_clarification_answer(
                job_id,
                conversation_id,
                answer=answer,
                raw=raw,
                structured_submission=structured_submission,
            )

            from services.event_wait_engine import (
                EventWaitEngine,
                CLARIFICATION_REPLY,
            )

            EventWaitEngine.signal(
                job_id,
                CLARIFICATION_REPLY,
                conversation_id=conversation_id,
                payload={
                    "reply": reply,
                    "structured_reply": structured_reply,
                    "answer": answer,
                },
            )
            return {
                "next_action": "resume",
                "message": "Clarification recorded. Resuming execution.",
            }

        # MODIFY → delegate to RequirementPatchEngine (specification mutation).
        # RequirementPatchEngine enforces frozen-spec immutability, idempotency,
        # and the max-patch-per-session limit.  record_clarification_answer()
        # is NOT used here.
        if intent in (ContinuationIntent.MODIFY,):
            from services.requirement_patch import (
                RequirementPatch,
                RequirementPatchEngine,
            )
            from models.agent import ProjectSpecification

            # Load current spec (frozen) from the jobs table.
            spec_dict = None
            try:
                from core.database import get_supabase_async
                result = (
                    await (await get_supabase_async())
                    .table("jobs")
                    .select("spec")
                    .eq("id", job_id)
                    .limit(1)
                    .execute()
                )
                if result.data and result.data[0].get("spec"):
                    spec_dict = result.data[0]["spec"]
            except Exception:
                spec_dict = None
            if not spec_dict:
                raise ConversationContinuationError(
                    "No specification found to patch"
                )
            spec = ProjectSpecification(**spec_dict)

            # Build the patch from the structured reply (frontend-parsed) or a
            # single-field update derived from the free-text reply.
            if structured_reply and structured_reply.get("patch"):
                patch = RequirementPatch(**structured_reply["patch"])
            else:
                # Minimal default: a single free-text modification note. The
                # caller (or a future parser) supplies the concrete field path.
                patch = RequirementPatch(
                    patch_type="update_field",
                    target_path="notes",
                    new_value=reply or "",
                )

            patched_spec = await RequirementPatchEngine.apply_patch(
                job_id,
                conversation_id,
                spec=spec,
                patch=patch,
            )
            # Persist the patched spec back to the jobs table.
            try:
                from core.database import get_supabase_async
                await (await get_supabase_async()) \
                    .table("jobs") \
                    .update({"spec": patched_spec.model_dump(mode="json")}) \
                    .eq("id", job_id) \
                    .execute()
            except Exception as exc:
                logger.warning("[continuation] spec persist failed: %s", exc)

            # Wake the EventWaitEngine orchestrator to resume with the patched
            # specification (full safety verify -> resume -> re-dispatch).
            from services.event_wait_engine import EventWaitEngine, USER_REPLY

            EventWaitEngine.signal(
                job_id,
                USER_REPLY,
                conversation_id=conversation_id,
                payload={"reply": reply, "structured_reply": structured_reply},
            )
            return {
                "next_action": "resume",
                "message": "Specification patched. Resuming execution.",
                "patch_id": patch.patch_id,
            }

        # Fallback (should not be reached)
        return {
            "next_action": "clarify",
            "message": "Please clarify your modification.",
        }
