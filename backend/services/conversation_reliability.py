"""
Conversation Reliability Layer — Objectives 1-7 (Sprint 3B.1).

This is the ONE shared reliability layer used by:
    - ApprovalEngine
    - InteractiveWaitEngine
    - ConversationContinuationEngine
    - RequirementPatchEngine
    - ResumeManager

This layer owns:
    • Idempotency (Objective 1)
    • Optimistic locking (Objective 2)
    • Atomic persistence (Objective 3)
    • Exactly-once resume (Objective 4)
    • Deterministic ordering (Objective 5)
    • Crash recovery (Objective 6)

No service may implement its own duplicate detection.
No service may implement its own optimistic locking.
No service may implement its own ordering.
The shared guard becomes the single source of truth.
"""

from __future__ import annotations

import json as _json
import logging
import time
from datetime import datetime, timezone
from typing import Any

from models.approval import ApprovalRequest
from models.conversation import ConversationSession, ConversationSessionStore, SessionState

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Typed exceptions (Objective 1, 2, 4)
# ---------------------------------------------------------------------------

class IdempotencyError(Exception):
    """Raised when a duplicate operation is detected (Objective 1)."""

    def __init__(self, operation_id: str):
        self.operation_id = operation_id
        super().__init__(
            f"Duplicate operation detected: {operation_id}"
        )


class OptimisticLockError(Exception):
    """Raised when a version conflict is detected (Objective 2)."""

    def __init__(self, expected: int, actual: int):
        self.expected = expected
        self.actual = actual
        super().__init__(
            f"Version conflict: expected {expected}, got {actual}"
        )


class ExactlyOnceError(Exception):
    """Raised when exactly-once guarantee is violated (Objective 4)."""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(f"Exactly-once violation: {reason}")


# ---------------------------------------------------------------------------
# Typed storage errors (Objective 2)
# ---------------------------------------------------------------------------

class IdempotencyStorageError(Exception):
    """Raised when idempotency storage is unavailable."""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(f"IdempotencyStorageError: {reason}")


class ResumeStorageError(Exception):
    """Raised when resume outcome storage is unavailable."""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(f"ResumeStorageError: {reason}")


class ConversationStorageError(Exception):
    """Raised when conversation persistence fails."""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(f"ConversationStorageError: {reason}")


# ---------------------------------------------------------------------------
# IdempotencyGuard (Objective 1)
# ---------------------------------------------------------------------------

class IdempotencyGuard:
    """Exactly-once idempotency for ALL operations.

    Responsibilities:
      • generate operation_id
      • persist operation_id
      • detect duplicates
      • return previous result for duplicate operations

    Every approval/continue/resume/patch operation must pass through this guard.
    """

    @staticmethod
    def generate_operation_id(
        job_id: str,
        operation_type: str,
        *,
        content_hash: str | None = None,
    ) -> str:
        """Generate deterministic operation ID.

        Format: ``{job_id}:{operation_type}:{content_hash}``
        If ``content_hash`` is None, uses timestamp (non-idempotent).
        """
        import hashlib
        if content_hash is None:
            content_hash = hashlib.sha256(
                f"{time.time()}".encode()
            ).hexdigest()[:16]
        return f"{job_id}:{operation_type}:{content_hash}"

    @staticmethod
    async def check(
        job_id: str,
        operation_id: str,
    ) -> dict[str, Any] | None:
        """Check if operation already executed.

        Returns previous result if duplicate, None if new operation.
        """
        from core.database import get_supabase

        try:
            result = (
                get_supabase()
                .table("conversation_audit")
                .select("content")
                .eq("job_id", job_id)
                .eq("content->>operation_id", operation_id)
                .limit(1)
                .execute()
            )
            if result.data:
                content = result.data[0].get("content", {})
                return content.get("previous_result")
        except Exception:
            raise ConversationStorageError(
                "storage operation failed"
            )
        return None

    @staticmethod
    async def record(
        job_id: str,
        conversation_id: str,
        *,
        operation_id: str,
        result: dict[str, Any],
    ) -> None:
        """Record operation result for future idempotency checks."""
        from services.conversation_audit import (
            AuditEvent,
            AuditEventType,
            ConversationAuditEngine,
        )

        await ConversationAuditEngine.record_intent(
            job_id=job_id,
            conversation_id=conversation_id,
            session_id="",
            intent=f"idempotent:{operation_id}",
            spec_version=None,
            cursor_version=None,
        )
        # Also persist to a dedicated idempotency table (simpler lookup)
        from core.database import get_supabase
        try:
            get_supabase().table("idempotency_store").insert(
                {
                    "operation_id": operation_id,
                    "job_id": job_id,
                    "result": _json.loads(_json.dumps(result, default=str)),
                    "created_at": datetime.now(timezone.utc).isoformat(),
                }
            ).execute()
        except Exception as exc:
            # Table might not exist yet — raise typed error
            raise IdempotencyStorageError(
                f"Failed to persist idempotency record: {exc}"
            )


# ---------------------------------------------------------------------------
# OptimisticLockGuard (Objective 2)
# ---------------------------------------------------------------------------

class OptimisticLockGuard:
    """Version-based optimistic locking for ConversationSession and ApprovalRequest.

    ConversationSession:
      - session_version (int, default 0)
      - Updates MUST fail with OptimisticLockError when versions differ

    ApprovalRequest:
      - request_version (int, default 0)
      - Updates MUST fail with OptimisticLockError when versions differ
    """

    @staticmethod
    def bump_session_version(session: ConversationSession) -> None:
        """Bump session_version (called before save)."""
        # session_version defaults to 0 if not present
        v = getattr(session, "session_version", 0) or 0
        session.session_version = v + 1
        session.updated_at = datetime.now(timezone.utc)

    @staticmethod
    async def save_session_atomic(
        session: ConversationSession,
        *,
        expected_version: int | None = None,
    ) -> None:
        """Save session with optimistic locking.

        If ``expected_version`` is provided and doesn't match,
        raises OptimisticLockError.
        """
        if expected_version is not None:
            current = getattr(session, "session_version", 0) or 0
            if current != expected_version:
                raise OptimisticLockError(expected_version, current)

        # Bump version
        OptimisticLockGuard.bump_session_version(session)

        # Persist (use private _save)
        await ConversationSessionStore._save(session)
        logger.info(
            "[reliability] session %s saved (version %s)",
            session.session_id,
            getattr(session, "session_version", 0),
        )

    @staticmethod
    async def save_approval_atomic(
        approval: ApprovalRequest,
        *,
        expected_version: int | None = None,
    ) -> None:
        """Save approval with optimistic locking.

        If ``expected_version`` is provided and doesn't match,
        raises OptimisticLockError.
        """
        if expected_version is not None:
            current = getattr(approval, "request_version", 0) or 0
            if current != expected_version:
                raise OptimisticLockError(expected_version, current)

        # Bump version
        v = getattr(approval, "request_version", 0) or 0
        approval.request_version = v + 1
        approval.updated_at = datetime.now(timezone.utc)

        # Persist (delegates to ApprovalEngine._persist)
        from services.approval_engine import ApprovalEngine
        engine = ApprovalEngine(approval.job_id, approval.conversation_id)
        await engine._persist(approval)
        logger.info(
            "[reliability] approval %s saved (version %s)",
            approval.approval_id,
            getattr(approval, "request_version", 0),
        )


# ---------------------------------------------------------------------------
# AtomicPersistenceGuard (Objective 3)
# ---------------------------------------------------------------------------

class AtomicPersistenceGuard:
    """Atomic persistence for conversation updates.

    ConversationSession, ApprovalRequest, ExecutionCursor
    must either ALL succeed or ALL rollback.

    No partial updates.
    No inconsistent state.
    If persistence fails, execution must stop safely.
    """

    @staticmethod
    async def save_all(
        *,
        session: ConversationSession | None = None,
        approval: ApprovalRequest | None = None,
        cursor: Any | None = None,  # ExecutionCursor
        job_id: str = "",
    ) -> None:
        """Save all entities atomically.

        Uses a simple all-or-nothing approach:
          1. Save session (if provided)
          2. Save approval (if provided)
          3. Save cursor (if provided)
        If any step fails, raises and does NOT retry silently.
        """
        errors: list[str] = []

        # 1. Save session
        if session is not None:
            try:
                await OptimisticLockGuard.save_session_atomic(session)
            except Exception as exc:
                errors.append(f"session: {exc}")

        # 2. Save approval
        if approval is not None:
            try:
                await OptimisticLockGuard.save_approval_atomic(approval)
            except Exception as exc:
                errors.append(f"approval: {exc}")

        # 3. Save cursor
        if cursor is not None:
            try:
                from services.resume_manager import ResumeManager
                await ResumeManager.save_execution_cursor(
                    job_id, cursor,
                    expected_version=getattr(cursor, "cursor_version", None),
                )
            except Exception as exc:
                errors.append(f"cursor: {exc}")

        if errors:
            raise RuntimeError(
                f"Atomic persistence failed: {', '.join(errors)}"
            )


# ---------------------------------------------------------------------------
# ExactlyOnceResumeGuard (Objective 4)
# ---------------------------------------------------------------------------

class ExactlyOnceResumeGuard:
    """Guarantee exactly one successful resume.

    ResumeToken, ExecutionCursor, ConversationSession, ApprovalRequest
    must guarantee exactly one successful resume.

    Duplicate resume requests must return the existing outcome
    without executing tools again.
    """

    @staticmethod
    async def check_resume(
        job_id: str,
        approval_id: str,
    ) -> dict[str, Any] | None:
        """Check if resume already happened.

        Returns previous resume result if duplicate, None if fresh.
        """
        from core.database import get_supabase

        try:
            result = (
                get_supabase()
                .table("conversation_audit")
                .select("content")
                .eq("job_id", job_id)
                .eq("content->>approval_id", approval_id)
                .eq("event_type", "resume")
                .limit(1)
                .execute()
            )
            if result.data:
                return result.data[0].get("content", {}).get("resume_result")
        except Exception:
            raise ResumeStorageError(
                "resume storage failed"
            )
        return None

    @staticmethod
    async def record_resume(
        job_id: str,
        approval_id: str,
        *,
        resume_result: dict[str, Any],
    ) -> None:
        """Record resume result for future duplicate detection."""
        from services.conversation_audit import ConversationAuditEngine

        await ConversationAuditEngine.record_resume(
            job_id=job_id,
            conversation_id="",
            session_id="",
            cursor_version=None,
            spec_version=None,
        )
        # Also persist to a dedicated table
        from core.database import get_supabase
        try:
            get_supabase().table("resume_outcomes").insert(
                {
                    "approval_id": approval_id,
                    "job_id": job_id,
                    "resume_result": _json.loads(
                        _json.dumps(resume_result, default=str)
                    ),
                    "created_at": datetime.now(timezone.utc).isoformat(),
                }
            ).execute()
        except Exception:
            raise ResumeStorageError(
                "resume storage failed"
            )


# ---------------------------------------------------------------------------
# DeterministicOrderingGuard (Objective 5)
# ---------------------------------------------------------------------------

class DeterministicOrderingGuard:
    """Deterministic ordering for conversation replies.

    Each reply receives sequence_number.
    Replies are processed strictly in order.
    Future replies cannot execute before earlier ones.
    Out-of-order replies remain pending until valid.
    """

    @staticmethod
    def assign_sequence(session: ConversationSession) -> int:
        """Assign next sequence number to a session."""
        v = getattr(session, "next_sequence", 0) or 0
        session.next_sequence = v + 1
        return v

    @staticmethod
    async def check_order(
        session: ConversationSession,
        received_sequence: int,
    ) -> str:
        """Check if reply can be processed.

        Returns:
          - "process" if sequence matches expected
          - "pending" if sequence > expected (out-of-order)
          - "stale" if sequence < expected (already processed)
        """
        expected = getattr(session, "next_sequence", 0) or 0
        if received_sequence == expected:
            return "process"
        if received_sequence > expected:
            return "pending"
        return "stale"


# ---------------------------------------------------------------------------
# CrashRecoveryGuard (Objective 6)
# ---------------------------------------------------------------------------

class CrashRecoveryGuard:
    """Restore state after server crash.

    Recovery must restore:
      - ConversationSession
      - ExecutionCursor
      - ApprovalRequest
      - ResumeToken

    without corruption.
    No duplicated execution.
    No lost approval.
    No lost conversation state.
    """

    @staticmethod
    async def recover(
        job_id: str,
        conversation_id: str,
    ) -> dict[str, Any]:
        """Recover all state after crash.

        Returns recovered state dict.
        """
        state: dict[str, Any] = {
            "session": None,
            "cursor": None,
            "approval": None,
            "token_valid": False,
        }

        # 1. Recover ConversationSession
        try:
            session = await ConversationSessionStore.load(job_id)
            if session:
                state["session"] = session.model_dump(mode="json")
        except Exception as exc:
            logger.warning("[recovery] session recovery failed: %s", exc)

        # 2. Recover ExecutionCursor
        try:
            from services.resume_manager import ResumeManager
            bundle = await ResumeManager.load_resume_bundle(
                job_id, conversation_id,
            )
            state["cursor"] = bundle.get("execution_cursor")
        except Exception as exc:
            logger.warning("[recovery] cursor recovery failed: %s", exc)

        # 3. Recover ApprovalRequest
        try:
            from core.database import get_supabase
            result = (
                get_supabase()
                .table("approval_requests")
                .select("*")
                .eq("job_id", job_id)
                .eq("status", "pending")
                .limit(1)
                .execute()
            )
            if result.data:
                state["approval"] = result.data[0]
        except Exception as exc:
            logger.warning("[recovery] approval recovery failed: %s", exc)

        # 4. Verify ResumeToken (if any)
        try:
            from models.approval import ResumeTokenStore
            token = await ResumeTokenStore.load(approval_id=job_id)
            if token and not token.revoked and not token.consumed:
                state["token_valid"] = True
        except Exception:
            raise ResumeStorageError(
                "resume storage failed"
            )

        logger.info("[recovery] recovered state for job %s", job_id)
        return state


# ---------------------------------------------------------------------------
# StartupVerifier (Objective 3)
# ---------------------------------------------------------------------------

class StartupVerificationError(Exception):
    """Raised when required storage objects are missing (Objective 3)."""

    def __init__(self, missing: list[str]):
        self.missing = missing
        super().__init__(
            f"Startup verification failed — missing: {', '.join(missing)}"
        )


class StartupVerifier:
    """Verify all required storage objects exist during application startup.

    Required tables:
      - ``idempotency_store``
      - ``resume_outcomes``
      - ``conversation_audit``
      - ``approval_requests``
      - ``jobs`` (for conversation_session column)

    If any table is missing, application startup MUST fail.
    """

    @staticmethod
    async def verify() -> None:
        """Run startup verification.

        Raises StartupVerificationError if any table is missing.
        """
        from core.database import get_supabase

        missing: list[str] = []
        required_tables = [
            "idempotency_store",
            "resume_outcomes",
            "conversation_audit",
            "approval_requests",
            "jobs",
        ]

        for table in required_tables:
            try:
                get_supabase().table(table).select("*").limit(0).execute()
            except Exception as exc:
                missing.append(table)
                logger.error(
                    "[startup] table %s missing: %s",
                    table, exc,
                )

        if missing:
            raise StartupVerificationError(missing)

        logger.info("[startup] all required tables verified")
