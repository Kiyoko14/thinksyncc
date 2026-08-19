"""
Conversation Audit — Objective 6 (Sprint 3B).

Persist every interaction.

Audit includes:
    Question
    Answer
    Intent
    Requirement Patch
    Approval
    Resume
    Execution
    Timestamp
    Actor
    Version

Every interaction must be reproducible.
"""

from __future__ import annotations
from pydantic import BaseModel, Field
from enum import Enum

import logging
import json as _json
from datetime import datetime, timezone
from typing import Any

from models.conversation import ConversationSession
from models.approval import ApprovalDecision, ApprovalStatus

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# AuditEvent
# ---------------------------------------------------------------------------

class AuditEventType(str, Enum):
    """Types of auditable events."""

    QUESTION = "question"
    ANSWER = "answer"
    INTENT = "intent"
    PATCH = "patch"
    APPROVAL = "approval"
    RESUME = "resume"
    EXECUTION = "execution"
    TIMEOUT = "timeout"
    CANCEL = "cancel"
    RESTART = "restart"


class AuditEvent(BaseModel):
    """A single auditable event."""

    event_id: str = Field(default_factory=lambda: __import__("uuid").uuid4().hex[:16])
    job_id: str = ""
    conversation_id: str = ""
    session_id: str = ""

    event_type: AuditEventType = AuditEventType.ANSWER
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    # Content
    actor: str = "user"  # "user" | "system" | "admin"
    content: dict[str, Any] = Field(default_factory=dict)

    # Versioning (for reproducibility)
    spec_version: int | None = None
    cursor_version: int | None = None

    # -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- --
    # Persistence
    # -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- --

    async def persist(self) -> None:
        """Persist to ``conversation_audit`` table."""
        from core.database import get_supabase, get_supabase_async

        try:
            await (await get_supabase_async()).table("conversation_audit").insert(
                json.loads(self.model_dump_json())
            ).execute()
        except Exception as exc:
            logger.error("[audit] failed to persist event: %s", exc)


# ---------------------------------------------------------------------------
# ConversationAuditEngine
# ---------------------------------------------------------------------------

class ConversationAuditEngine:
    """Persist every interaction.

    Every audit event includes:
      - Question / Answer / Intent / Patch / Approval / Resume
      - Timestamp
      - Actor
      - Version (spec + cursor)
    """

    @classmethod
    async def record_question(
        cls,
        job_id: str,
        conversation_id: str,
        *,
        session_id: str,
        question: dict[str, Any],
        spec_version: int | None = None,
    ) -> None:
        """Record a clarification question."""
        event = AuditEvent(
            job_id=job_id,
            conversation_id=conversation_id,
            session_id=session_id,
            event_type=AuditEventType.QUESTION,
            actor="system",
            content={"question": question},
            spec_version=spec_version,
        )
        await event.persist()
        logger.info("[audit] question recorded for job %s", job_id)

    @classmethod
    async def record_answer(
        cls,
        job_id: str,
        conversation_id: str,
        *,
        session_id: str,
        answer: dict[str, Any],
        spec_version: int | None = None,
    ) -> None:
        """Record a user answer."""
        event = AuditEvent(
            job_id=job_id,
            conversation_id=conversation_id,
            session_id=session_id,
            event_type=AuditEventType.ANSWER,
            actor="user",
            content={"answer": answer},
            spec_version=spec_version,
        )
        await event.persist()
        logger.info("[audit] answer recorded for job %s", job_id)

    @classmethod
    async def record_intent(
        cls,
        job_id: str,
        conversation_id: str,
        *,
        session_id: str,
        intent: str,
        spec_version: int | None = None,
        cursor_version: int | None = None,
    ) -> None:
        """Record a user intent (continue / approve / reject / modify)."""
        event = AuditEvent(
            job_id=job_id,
            conversation_id=conversation_id,
            session_id=session_id,
            event_type=AuditEventType.INTENT,
            actor="user",
            content={"intent": intent},
            spec_version=spec_version,
            cursor_version=cursor_version,
        )
        await event.persist()
        logger.info("[audit] intent %s recorded for job %s", intent, job_id)

    @classmethod
    async def record_patch(
        cls,
        job_id: str,
        conversation_id: str,
        *,
        session_id: str,
        patch: dict[str, Any],
        spec_version: int | None = None,
    ) -> None:
        """Record a requirement patch."""
        event = AuditEvent(
            job_id=job_id,
            conversation_id=conversation_id,
            session_id=session_id,
            event_type=AuditEventType.PATCH,
            actor="user",
            content={"patch": patch},
            spec_version=spec_version,
        )
        await event.persist()
        logger.info("[audit] patch recorded for job %s", job_id)

    @classmethod
    async def record_approval(
        cls,
        job_id: str,
        conversation_id: str,
        *,
        session_id: str,
        approval_id: str,
        decision: str,
        reason: str = "",
        spec_version: int | None = None,
    ) -> None:
        """Record an approval decision."""
        event = AuditEvent(
            job_id=job_id,
            conversation_id=conversation_id,
            session_id=session_id,
            event_type=AuditEventType.APPROVAL,
            actor="user",
            content={
                "approval_id": approval_id,
                "decision": decision,
                "reason": reason,
            },
            spec_version=spec_version,
        )
        await event.persist()
        logger.info("[audit] approval %s recorded for job %s", decision, job_id)

    @classmethod
    async def record_resume(
        cls,
        job_id: str,
        conversation_id: str,
        *,
        session_id: str,
        cursor_version: int | None = None,
        spec_version: int | None = None,
    ) -> None:
        """Record a resume event."""
        event = AuditEvent(
            job_id=job_id,
            conversation_id=conversation_id,
            session_id=session_id,
            event_type=AuditEventType.RESUME,
            actor="system",
            content={"cursor_version": cursor_version},
            spec_version=spec_version,
            cursor_version=cursor_version,
        )
        await event.persist()
        logger.info("[audit] resume recorded for job %s", job_id)

    @classmethod
    async def record_timeout(
        cls,
        job_id: str,
        conversation_id: str,
        *,
        session_id: str,
    ) -> None:
        """Record a timeout event."""
        event = AuditEvent(
            job_id=job_id,
            conversation_id=conversation_id,
            session_id=session_id,
            event_type=AuditEventType.TIMEOUT,
            actor="system",
        )
        await event.persist()
        logger.info("[audit] timeout recorded for job %s", job_id)

    @classmethod
    async def get_audit_trail(
        cls,
        job_id: str,
        conversation_id: str | None = None,
    ) -> list[AuditEvent]:
        """Reproduce the full interaction history."""
        from core.database import get_supabase_async

        query = (
            (await get_supabase_async())
            .table("conversation_audit")
            .select("*")
            .eq("job_id", job_id)
        )
        if conversation_id:
            query = query.eq("conversation_id", conversation_id)
        query = query.order("timestamp", desc=False)
        result = await query.execute()
        return [AuditEvent(**row) for row in (result.data or [])]
