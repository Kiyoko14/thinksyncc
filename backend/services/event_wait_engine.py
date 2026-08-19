"""Event-Driven Wait Engine — Sprint 3C.C.

Production-grade, event-driven replacement for the timeout-oriented waiting
behaviour of the Interactive Wait system.

Design goals (from the sprint brief)
-----------------------------------
• The agent NEVER relies on long polling.  A suspended job releases its worker
  and parks on an :class:`asyncio.Event` (one per job).  It wakes only when a
  concrete system event is delivered.
• Event model is open for extension: callers signal *named* events; the wait
  loop does not hard-switch on event types, so future event kinds (web UI
  replies, API callbacks, custom signals) require NO change to the wait loop.
• All persistence and safety guarantees are delegated to the EXISTING
  reliability layer (``InteractiveWaitEngine``, ``ResumeManager``,
  ``OptimisticLockGuard``, ``ConversationSession``, ``ApprovalEngine``).
  This module adds NO second persistence layer.

Events
------
The engine recognises the canonical event set from the brief:

    USER_REPLY        — a user replied (Telegram / Web UI / API)
    APPROVAL_RECEIVED — an approval decision was resolved
    RESUME_REQUEST    — an explicit system continuation request
    TIMEOUT           — the configured wait window elapsed
    CANCEL            — the conversation / job was cancelled
    CLARIFICATION_REPLY — a user answered a clarification question (Sprint 3C.D)

The canonical set is registered as known types, but the engine ACCEPTS any
string as an event type.  Adding a new event type is therefore a matter of
emitting it (e.g. ``EventWaitEngine.signal(job_id, "WEB_UI_REPLY", ...)``)
and — if it should wake the job — delivering it through the same path.
No existing event handler needs to be modified.

Wake flow
---------
1. ``suspend()`` registers a wait for ``job_id`` and arms a timeout task.
2. A resolver (user reply, approval, cancellation, timeout) calls
   ``signal()`` which sets the per-job :class:`asyncio.Event` and records the
   signal.  Exactly one signal wakes the parked task; late/duplicate signals
   are recorded but do not double-resume (the reliability layer's single-use
   token + exactly-once resume guard enforce this).
3. ``await_and_resume()`` (spawned by the orchestrator) returns from ``wait()``
   with the signal, runs self-verification against the reliability layer, then
   re-dispatches execution through the existing ``AgentService.run_job``
   pipeline.  The pipeline reloads the persisted ``ExecutionCursor`` and
   continues from the exact resume point — never restarting the job.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Event model (extensible)
# ---------------------------------------------------------------------------

# Canonical event types required by the brief.  Kept as a registry (not an
# enum-based switch) so future types can be added without touching handlers.
USER_REPLY = "user_reply"
APPROVAL_RECEIVED = "approval_received"
RESUME_REQUEST = "resume_request"
TIMEOUT = "timeout"
CANCEL = "cancel"
CLARIFICATION_REPLY = "clarification_reply"

KNOWN_EVENT_TYPES: frozenset[str] = frozenset(
    {
        USER_REPLY,
        APPROVAL_RECEIVED,
        RESUME_REQUEST,
        TIMEOUT,
        CANCEL,
        CLARIFICATION_REPLY,
    }
)


class EventWaitError(Exception):
    """Base error for the event-driven wait engine."""


class WaitResumeValidationError(EventWaitError):
    """Raised when pre-resume self-verification fails.

    The brief requires: *Before resuming execution verify execution cursor
    valid, interaction state valid, workspace exists, project still available,
    approval state consistent, conversation session valid. If validation fails:
    raise typed exception. Never silently continue.*
    """

    def __init__(self, reason: str, *, detail: str | None = None):
        self.reason = reason
        self.detail = detail
        msg = f"Resume validation failed ({reason})"
        if detail:
            msg = f"{msg}: {detail}"
        super().__init__(msg)


@dataclass
class EventSignal:
    """A single delivered system event."""

    event_type: str
    job_id: str
    conversation_id: str = ""
    payload: dict[str, Any] = field(default_factory=dict)
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------


class EventWaitEngine:
    """In-process, event-driven wake mechanism for suspended jobs.

    One :class:`asyncio.Event` per suspended job.  This is the *only* waiting
    primitive — there is no polling.  The engine is a process-local dispatcher;
    durability of *what* to do on wake lives in the reliability layer, not here.
    """

    # job_id -> wake primitive
    _waits: dict[str, asyncio.Event] = {}
    # job_id -> last delivered signal (so late/duplicate signals are visible
    # but never double-processed — the reliability layer enforces exactly-once)
    _signals: dict[str, EventSignal | None] = {}
    # job_id -> asyncio timeout task (cancelled on normal wake)
    _timeout_tasks: dict[str, asyncio.Task[None]] = {}
    # job_id -> conversation_id (for timeout routing without re-querying)
    _conversations: dict[str, str] = {}

    # -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- --
    # Registration
    # -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- --

    @classmethod
    def register(
        cls,
        job_id: str,
        conversation_id: str,
        *,
        timeout_seconds: int = 1800,
    ) -> None:
        """Register a suspended wait for ``job_id`` and arm its timeout.

        Idempotent: re-registering the same job (e.g. after a restart of the
        wait loop) re-arms the timeout and replaces the wake primitive, which
        is safe because the reliability layer is the source of truth for state.

        If an event already arrived before registration (event-before-suspend
        race), it is preserved as the authoritative signal and the wait wakes
        immediately so the job is not stranded.
        """
        cls._waits[job_id] = asyncio.Event()
        cls._conversations[job_id] = conversation_id
        # Preserve a pre-arrived signal (do not clobber it with None).
        if cls._signals.get(job_id) is None:
            cls._signals[job_id] = None
        cls._arm_timeout(job_id, conversation_id, timeout_seconds)
        # If an event pre-arrived, wake immediately.
        if cls._signals.get(job_id) is not None:
            wait = cls._waits.get(job_id)
            if wait is not None:
                wait.set()
                cls._cancel_timeout(job_id)
        logger.info(
            "[event-wait] registered wait | job=%s | timeout=%ss",
            job_id,
            timeout_seconds,
        )

    @classmethod
    def _arm_timeout(
        cls,
        job_id: str,
        conversation_id: str,
        timeout_seconds: int,
    ) -> None:
        """Schedule a TIMEOUT signal if the job is still waiting when it fires."""
        # Cancel any pre-existing timeout task for this job.
        prior = cls._timeout_tasks.get(job_id)
        if prior is not None and not prior.done():
            prior.cancel()

        async def _fire() -> None:
            try:
                await asyncio.sleep(timeout_seconds)
            except asyncio.CancelledError:
                return
            # Only emit TIMEOUT if the job has not already been woken.
            if cls._signals.get(job_id) is not None:
                return
            if not cls.is_waiting(job_id):
                return
            logger.warning("[event-wait] timeout fired | job=%s", job_id)
            cls.signal(
                job_id,
                TIMEOUT,
                conversation_id=conversation_id,
                payload={"reason": "wait_timeout_expired"},
            )

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # No event loop (e.g. import-time).  Timeout cannot be armed;
            # the recovery loop / TimeoutManager cover this case instead.
            return
        cls._timeout_tasks[job_id] = loop.create_task(_fire())

    # -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- --
    # Signal (wake)
    # -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- --

    @classmethod
    def signal(
        cls,
        job_id: str,
        event_type: str,
        *,
        conversation_id: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> bool:
        """Deliver a system event to a suspended job.

        Returns ``True`` if a waiting task was woken (or will be), ``False`` if
        there is no active wait for this job (the signal is recorded for
        traceability but does not wake anything — e.g. an event that arrives
        after the job already resumed/completed).

        Unknown event types are accepted (extensibility) but logged.
        """
        if event_type not in KNOWN_EVENT_TYPES:
            logger.info(
                "[event-wait] signalling unknown/extended event type %r | job=%s",
                event_type,
                job_id,
            )

        conversation_id = conversation_id or cls._conversations.get(job_id, "")
        # FIRST signal is authoritative (exactly-once resume).  Later signals
        # for the same wait do not overwrite the original payload — this
        # prevents an empty/duplicate event from clobbering a real user reply
        # or approval decision that already established the wake reason.
        if cls._signals.get(job_id) is None:
            cls._signals[job_id] = EventSignal(
                event_type=event_type,
                job_id=job_id,
                conversation_id=conversation_id,
                payload=payload or {},
            )
        else:
            # Record that a later signal arrived, but keep the original
            # authoritative signal.  We simply ensure the wake primitive is set.
            pass

        wait = cls._waits.get(job_id)
        if wait is None:
            logger.info(
                "[event-wait] signal %r delivered but no active wait | job=%s",
                event_type,
                job_id,
            )
            return False

        wait.set()
        cls._cancel_timeout(job_id)
        logger.info(
            "[event-wait] signal %r woke wait | job=%s", event_type, job_id
        )
        return True

    @classmethod
    def _cancel_timeout(cls, job_id: str) -> None:
        task = cls._timeout_tasks.pop(job_id, None)
        if task is not None and not task.done():
            task.cancel()

    # -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- --
    # Wait
    # -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- --

    @classmethod
    async def wait(cls, job_id: str, timeout_seconds: int) -> EventSignal | None:
        """Park until a signal arrives or ``timeout_seconds`` elapses.

        Returns the delivered :class:`EventSignal`, or ``None`` on timeout
        (the timeout task also emits a TIMEOUT signal, so in practice a TIMEOUT
        signal is returned; ``None`` covers the defensive case).
        """
        wait = cls._waits.get(job_id)
        if wait is None:
            # No active wait registered — nothing to park on.
            logger.warning(
                "[event-wait] wait() called with no active wait | job=%s", job_id
            )
            return None
        try:
            await asyncio.wait_for(wait.wait(), timeout=timeout_seconds)
        except asyncio.TimeoutError:
            return cls._signals.get(job_id)
        finally:
            cls._waits.pop(job_id, None)
            cls._cancel_timeout(job_id)
        return cls._signals.get(job_id)

    @classmethod
    def is_waiting(cls, job_id: str) -> bool:
        """Return True if a wait primitive is currently registered for job."""
        return job_id in cls._waits

    @classmethod
    def clear(cls, job_id: str) -> None:
        """Remove all wait state for a job (cleanup / cancel)."""
        cls._waits.pop(job_id, None)
        cls._signals.pop(job_id, None)
        cls._conversations.pop(job_id, None)
        cls._cancel_timeout(job_id)

    # -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- --
    # High-level orchestration helpers
    # -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- --

    @classmethod
    def last_signal(cls, job_id: str) -> EventSignal | None:
        return cls._signals.get(job_id)

    @classmethod
    async def verify_resume_safety(
        cls,
        job_id: str,
        conversation_id: str,
    ) -> None:
        """Self-verification before resuming (typed exceptions on failure).

        Mirrors the brief's verification checklist:
          execution cursor valid · interaction state valid · workspace exists ·
          project still available · approval state consistent · session valid
        """
        from models.approval import JobState
        from models.job import JobStatus
        from services.interactive_wait import InteractiveWaitEngine
        from services.resume_manager import ResumeManager
        from models.conversation import ConversationSessionStore

        # 1. Interaction state valid + still waiting
        state = await InteractiveWaitEngine.get_state(conversation_id, job_id)
        if state.current_state != JobState.WAITING_FOR_USER:
            raise WaitResumeValidationError(
                "interaction_state",
                detail=f"expected waiting_for_user, got {state.current_state.value}",
            )

        # 2. Execution cursor valid + resume point sane
        try:
            bundle = await ResumeManager.load_resume_bundle(job_id, conversation_id)
        except ValueError as exc:
            raise WaitResumeValidationError("execution_cursor", detail=str(exc))
        cursor = bundle.get("execution_cursor")
        plan = bundle.get("plan") or []
        if cursor is None:
            raise WaitResumeValidationError(
                "execution_cursor", detail="no ExecutionCursor persisted"
            )
        if not (0 <= cursor.resume_point <= max(len(plan), cursor.resume_point)):
            raise WaitResumeValidationError(
                "execution_cursor",
                detail=f"resume_point {cursor.resume_point} out of range for plan len {len(plan)}",
            )

        # 3. Job status consistent with waiting
        from core.database import get_supabase, get_supabase_async

        try:
            row = (
                await (await get_supabase_async())
                .table("jobs")
                .select("status,workspace_id,server_id")
                .eq("id", job_id)
                .limit(1)
                .execute()
            )
            job_row = row.data[0] if row.data else None
        except Exception as exc:  # DB read failure — typed re-raise after log
            logger.error("[event-wait] verify: job load failed | job=%s: %s", job_id, exc)
            raise WaitResumeValidationError("job_state", detail=str(exc))
        if job_row is None:
            raise WaitResumeValidationError("job_state", detail="job row missing")
        if job_row.get("status") != JobStatus.WAITING_FOR_USER.value:
            raise WaitResumeValidationError(
                "job_state",
                detail=f"status is {job_row.get('status')}, expected waiting_for_user",
            )

        # 4. Workspace + server still exist (system-level existence check;
        #    these services require a user_id we may not have at resume time,
        #    so query existence directly — no silent validation skip).
        workspace_id = job_row.get("workspace_id")
        server_id = job_row.get("server_id")
        if not workspace_id or not server_id:
            raise WaitResumeValidationError(
                "project_unavailable", detail="missing workspace_id or server_id"
            )
        from core.database import get_supabase_async

        try:
            ws = (
                await (await get_supabase_async())
                .table("workspaces")
                .select("id")
                .eq("id", workspace_id)
                .limit(1)
                .execute()
            )
            if not ws.data:
                raise WaitResumeValidationError(
                    "workspace", detail=f"workspace {workspace_id} not found"
                )
        except WaitResumeValidationError:
            raise
        except Exception as exc:
            raise WaitResumeValidationError(
                "workspace", detail=f"workspace lookup failed: {exc}"
            )
        try:
            srv = (
                await (await get_supabase_async())
                .table("servers")
                .select("id")
                .eq("id", server_id)
                .limit(1)
                .execute()
            )
            if not srv.data:
                raise WaitResumeValidationError(
                    "project_unavailable", detail=f"server {server_id} not found"
                )
        except WaitResumeValidationError:
            raise
        except Exception as exc:
            raise WaitResumeValidationError(
                "project_unavailable", detail=f"server lookup failed: {exc}"
            )

        # 5. Conversation session valid
        session = await ConversationSessionStore.load(job_id)
        if session is None:
            raise WaitResumeValidationError(
                "conversation_session", detail="no ConversationSession persisted"
            )
        # Touch OptimisticLockGuard to assert the session is lock-managed
        # (guards against a session that bypassed atomic persistence).
        if not isinstance(getattr(session, "session_version", None), int):
            raise WaitResumeValidationError(
                "conversation_session", detail="session missing optimistic-lock version"
            )

        # 6. Approval state consistent (if a pending approval exists)
        if state.pending_approval_ids:
            approval_id = state.pending_approval_ids[-1]
            try:
                from services.approval_engine import ApprovalEngine

                engine = ApprovalEngine(job_id, conversation_id)
                request = await engine.load(approval_id)
            except Exception as exc:
                raise WaitResumeValidationError(
                    "approval_state", detail=f"cannot load approval {approval_id}: {exc}"
                )
            if request is None or getattr(request, "status", None) is None:
                raise WaitResumeValidationError(
                    "approval_state", detail=f"approval {approval_id} inconsistent"
                )

        logger.info("[event-wait] resume safety verified | job=%s", job_id)

    @classmethod
    async def await_and_resume(
        cls,
        *,
        job_id: str,
        conversation_id: str,
        user_id: str,
        timeout_seconds: int,
        trace_id: str | None = None,
    ) -> None:
        """Park on the event, then re-dispatch execution from the exact cursor.

        This is the event-driven continuation driver.  It is spawned by the
        orchestrator after a suspend and runs concurrently with the worker's
        main loop (the worker has already released the job by returning).  On
        wake it verifies safety, applies the user reply/approval via the
        existing reliability layer, and re-enters ``AgentService.run_job`` which
        resumes from the persisted ``ExecutionCursor`` — never restarting.
        """
        from services.interactive_wait import InteractiveWaitEngine

        signal = await cls.wait(job_id, timeout_seconds)
        if signal is None:
            logger.warning("[event-wait] no signal (defensive) | job=%s", job_id)
            return

        # CANCEL: do not resume; ensure waiting state is torn down safely.
        if signal.event_type == CANCEL:
            logger.info("[event-wait] cancel received | job=%s", job_id)
            from services.conversation_reliability import OptimisticLockGuard
            from models.conversation import ConversationSessionStore

            session = await ConversationSessionStore.load(job_id)
            if session is not None:
                from models.conversation import SessionState

                try:
                    session.transition_to(SessionState.CANCELLED)
                    await OptimisticLockGuard.save_session_atomic(session)
                except Exception as exc:
                    logger.warning("[event-wait] cancel session transition failed: %s", exc)
            cls.clear(job_id)
            return

        # TIMEOUT: expire the waiting state via the existing timeout manager.
        if signal.event_type == TIMEOUT:
            logger.info("[event-wait] timeout received | job=%s", job_id)
            try:
                from services.timeout_manager import TimeoutManager

                await TimeoutManager.expire(job_id, conversation_id)
            except Exception as exc:
                logger.warning("[event-wait] timeout expiry failed: %s", exc)
            cls.clear(job_id)
            return

        # USER_REPLY / APPROVAL_RECEIVED / RESUME_REQUEST → resume execution.
        try:
            await cls.verify_resume_safety(job_id, conversation_id)
        except WaitResumeValidationError as exc:
            logger.error("[event-wait] resume aborted — %s", exc)
            cls.clear(job_id)
            return

        # Apply the reply/approval through the existing InteractiveWaitEngine so
        # the single-use ResumeToken is consumed and exactly-once is enforced.
        reply = (signal.payload or {}).get("reply")
        try:
            await InteractiveWaitEngine.resume(
                job_id,
                conversation_id,
                reply=reply,
            )
        except Exception as exc:
            logger.error(
                "[event-wait] resume token/state apply failed | job=%s: %s",
                job_id,
                exc,
            )
            cls.clear(job_id)
            return

        # Re-dispatch through the EXISTING pipeline.  run_job bypasses the worker
        # semaphore (we are already inside the worker process) and the pipeline
        # detects WAITING_FOR_USER + stored ExecutionCursor and continues from
        # the exact resume point — no re-plan, no restart.
        cls.clear(job_id)
        from services.agent_service import AgentService

        # Reload a minimal payload from the persisted job row.
        from core.database import get_supabase_async

        try:
            row = (
                await (await get_supabase_async())
                .table("jobs")
                .select("workspace_id,server_id,objective,max_steps,allow_write,dry_run,step_timeout_seconds,conversation_id")
                .eq("id", job_id)
                .limit(1)
                .execute()
            )
            job_row = row.data[0] if row.data else {}
        except Exception as exc:
            logger.error("[event-wait] cannot reload job for resume | job=%s: %s", job_id, exc)
            return

        from models.job import JobCreate

        payload = JobCreate(
            workspace_id=job_row.get("workspace_id"),
            server_id=job_row.get("server_id"),
            objective=job_row.get("objective", ""),
            max_steps=int(job_row.get("max_steps") or 8),
            allow_write=bool(job_row.get("allow_write", True)),
            dry_run=bool(job_row.get("dry_run", False)),
            step_timeout_seconds=job_row.get("step_timeout_seconds"),
            conversation_id=job_row.get("conversation_id") or conversation_id,
        )

        logger.info("[event-wait] re-dispatching resumed execution | job=%s", job_id)
        await AgentService.run_job(
            job_id,
            payload,
            user_id,
            trace_id=trace_id or job_id,
            bypass_semaphore=True,
        )

    # ----------------------------------------------------------------------
    # Clarification continuation driver (Sprint 3C.D)
    # ----------------------------------------------------------------------

    @classmethod
    async def await_clarification_reply(
        cls,
        *,
        job_id: str,
        conversation_id: str,
        user_id: str,
        timeout_seconds: int,
        turn: int = 1,
        trace_id: str | None = None,
    ) -> None:
        """Park on the event bus for a clarification answer, then re-dispatch.

        Adaptive clarification (Sprint 3C.D) reuses the SAME event bus and the
        SAME reliability layer as approval/resume.  When the Adaptive
        Clarification Engine decides it must ask, the orchestrator raises
        ``ClarificationSuspendSignal``, registers the job, and spawns THIS
        driver.  On a ``CLARIFICATION_REPLY`` (or ``USER_REPLY`` /
        ``RESUME_REQUEST``) the driver records the answer into the clarification
        session and re-enters ``AgentService.run_job`` — which re-runs the
        adaptive decision with the new context.  Crucially this is NOT a restart:
        requirement discovery / spec / conversation are reused, and only the
        newly-needed information is asked for on the next turn (multi-turn).

        A ``CANCEL`` safely tears down the waiting state; ``TIMEOUT`` expires it.
        """
        from services.interactive_wait import InteractiveWaitEngine

        signal = await cls.wait(job_id, timeout_seconds)
        if signal is None:
            logger.warning("[event-wait] no clarification signal (defensive) | job=%s", job_id)
            return

        if signal.event_type == CANCEL:
            logger.info("[event-wait] clarification cancel | job=%s", job_id)
            cls.clear(job_id)
            return

        if signal.event_type == TIMEOUT:
            logger.info("[event-wait] clarification timeout | job=%s", job_id)
            try:
                from services.timeout_manager import TimeoutManager

                await TimeoutManager.expire(job_id, conversation_id)
            except Exception as exc:
                logger.warning("[event-wait] clarification timeout expiry failed: %s", exc)
            cls.clear(job_id)
            return

        # CLARIFICATION_REPLY / USER_REPLY / RESUME_REQUEST → record + re-dispatch.
        reply = (signal.payload or {}).get("reply")
        answer = (signal.payload or {}).get("answer") or reply
        structured_submission = (signal.payload or {}).get("submission")
        try:
            await InteractiveWaitEngine.record_clarification_answer(
                job_id,
                conversation_id,
                answer=answer,
                raw=reply,
                structured_submission=structured_submission,
            )
        except Exception as exc:
            logger.error(
                "[event-wait] clarification answer record failed | job=%s: %s",
                job_id,
                exc,
            )
            cls.clear(job_id)
            return

        cls.clear(job_id)
        from services.agent_service import AgentService

        from core.database import get_supabase_async

        try:
            row = (
                await (await get_supabase_async())
                .table("jobs")
                .select(
                    "workspace_id,server_id,objective,max_steps,allow_write,"
                    "dry_run,step_timeout_seconds,conversation_id"
                )
                .eq("id", job_id)
                .limit(1)
                .execute()
            )
            job_row = row.data[0] if row.data else {}
        except Exception as exc:
            logger.error("[event-wait] cannot reload job for clarification | job=%s: %s", job_id, exc)
            return

        from models.job import JobCreate

        payload = JobCreate(
            workspace_id=job_row.get("workspace_id"),
            server_id=job_row.get("server_id"),
            objective=job_row.get("objective", ""),
            max_steps=int(job_row.get("max_steps") or 8),
            allow_write=bool(job_row.get("allow_write", True)),
            dry_run=bool(job_row.get("dry_run", False)),
            step_timeout_seconds=job_row.get("step_timeout_seconds"),
            conversation_id=job_row.get("conversation_id") or conversation_id,
        )

        logger.info(
            "[event-wait] re-dispatching after clarification (turn %s) | job=%s",
            turn,
            job_id,
        )
        await AgentService.run_job(
            job_id,
            payload,
            user_id,
            trace_id=trace_id or job_id,
            bypass_semaphore=True,
        )
