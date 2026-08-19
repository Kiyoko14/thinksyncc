"""Approval models - Sprint 3: Human-in-the-Loop Orchestration."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


class ApprovalConfigurationError(Exception):
    """Raised when approval subsystem configuration is invalid.

    Used for startup validation (Task 3 — Sprint 3A.3).
    Replaces generic RuntimeError with a typed, easily-identifiable exception.
    """

    def __init__(self, message: str):
        self.message = message
        super().__init__(f"ApprovalConfigurationError: {message}")


# ---------------------------------------------------------------------------
# Task 2 (Sprint 3A.3) — Global FrozenSpecification Guard
# ---------------------------------------------------------------------------

def ensure_frozen_spec_immutable(
    spec: Any,
    context: str = "orchestration",
) -> None:
    """Single global guard: raise if ``spec`` is frozen/approved.

    This is THE ONE shared check. No service may implement its own
    frozen check — they must all call this function.

    Args:
        spec: ``ProjectSpecification`` (pydantic model, dict, or None)
        context: human-readable context for the error

    Raises:
        FrozenSpecViolationError: if ``spec`` is frozen (approved)
    """
    if spec is None:
        return
    frozen = False
    if isinstance(spec, dict):
        frozen = bool(spec.get("frozen", False))
    else:
        frozen = bool(getattr(spec, "frozen", False))
    if frozen:
        spec_name = (
            spec.get("name", "<unknown>")
            if isinstance(spec, dict)
            else getattr(spec, "name", "<unknown>")
        )
        raise FrozenSpecViolationError(
            f"Cannot mutate frozen specification in {context}. "
            f"Specification '{spec_name}' is frozen. "
            f"Use resume to continue execution from the approved plan."
        )


class FrozenSpecViolationError(Exception):
    """Raised when any component tries to mutate a frozen specification."""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(f"FrozenSpecViolation: {reason}")


# ---------------------------------------------------------------------------
# Task 2 (Sprint 3A.2) — Shared approved-plan immutability helper
# ---------------------------------------------------------------------------
# DEPRECATED: kept for backward compatibility; delegates to
# ``ensure_frozen_spec_immutable()``.

def ensure_approved_plan_immutable(
    spec: Any,
    context: str = "orchestration",
) -> None:
    """[DEPRECATED] Use ``ensure_frozen_spec_immutable()`` instead."""
    ensure_frozen_spec_immutable(spec, context)


# ---------------------------------------------------------------------------
# Objective 1 - ApprovalDecision
# ---------------------------------------------------------------------------

class ApprovalDecision(str, Enum):
    """Final decision on an approval request."""

    APPROVED = "approved"
    REJECTED = "rejected"
    SKIPPED = "skipped"  # user said "use default" or "skip"


# ---------------------------------------------------------------------------
# Objective 2 - ApprovalStatus
# ---------------------------------------------------------------------------

class ApprovalStatus(str, Enum):
    """Lifecycle status of an approval request."""

    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    SKIPPED = "skipped"
    EXPIRED = "expired"


# ---------------------------------------------------------------------------
# Objective 3 - ApprovalType
# ---------------------------------------------------------------------------

class ApprovalType(str, Enum):
    """Category of action requiring approval."""

    REQUIREMENT = "requirement"
    ASSUMPTION = "assumption"
    PATCH = "patch"
    COMMAND = "command"
    SECRET = "secret"
    FILE_OVERWRITE = "file_overwrite"
    DEPLOYMENT = "deployment"
    DESTRUCTIVE = "destructive"
    EXECUTION = "execution"


# ---------------------------------------------------------------------------
# Objective 4 - ApprovalRequest
# ---------------------------------------------------------------------------

class ApprovalRequest(BaseModel):
    """A single action that requires user confirmation.

    Stored in ``approval_requests`` table.
    """

    approval_id: str = Field(default_factory=lambda: __import__("uuid").uuid4().hex[:16])
    job_id: str
    conversation_id: str
    approval_type: ApprovalType
    status: ApprovalStatus = ApprovalStatus.PENDING

    # What is being approved
    title: str = ""
    description: str = ""
    risk_level: str = "medium"  # low | medium | high | critical
    affected_files: list[str] = Field(default_factory=list)
    affected_commands: list[str] = Field(default_factory=list)
    affected_assumptions: list[str] = Field(default_factory=list)

    # Context needed for the user to make a decision
    context: dict[str, Any] = Field(default_factory=dict)

    # Audit
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    resolved_at: datetime | None = None
    resolved_by: str = ""  # user ID or "system"

    # Schema parity (db/schema.sql approval_requests.updated_at). Written by
    # ApprovalEngine._persist (services/approval_engine.py:297) and persisted via
    # model_dump_json(); the column is added by migration 20260715_schema_drift_fix.sql.
    updated_at: datetime | None = None

    # Reliability (Sprint 3B.1)
    request_version: int = 0  # optimistic locking (Objective 2)
    decision: ApprovalDecision | None = None
    reason: str = ""

    # Versioning (Sprint 2 compatibility)
    spec_version: int | None = None
    requirement_version: int | None = None


# ---------------------------------------------------------------------------
# Objective 5 - ApprovalAuditEvent
# ---------------------------------------------------------------------------

class ApprovalAuditEvent(BaseModel):
    """Immutable audit entry for every approval action.

    Stored in ``approval_audit`` table.
    """

    event_id: str = Field(default_factory=lambda: __import__("uuid").uuid4().hex[:16])
    approval_id: str
    job_id: str
    conversation_id: str
    event_type: str  # "created" | "approved" | "rejected" | "skipped" | "expired"
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    decision: ApprovalDecision | None = None
    reason: str = ""
    user: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# FIX 1 - ResumeToken (Sprint 3A.1)
# ---------------------------------------------------------------------------

class ResumeToken(BaseModel):
    """Single-use cryptographic token binding approval <-> resume.

    Prevents stale/duplicated resume requests from resuming an invalid state.

    Fields:
      - ``approval_id``       — which approval this token belongs to
      - ``execution_cursor_version`` — ``ExecutionCursor.cursor_version`` at issue time
      - ``specification_version`` — ``FrozenSpecification.version`` at issue time
      - ``issued_at``      — UTC timestamp when token was issued
      - ``expires_at``     — UTC timestamp after which token is invalid
      - ``nonce``            — random hex to prevent replay
      - ``signature``       — HMAC-SHA256 over canonical JSON (using ``APPROVAL_RESUME_SECRET``)
      - ``consumed``        — whether this token has already been used (single-use)
      - ``revoked``         — whether this token has been revoked (Task 3)
      - ``revocation_reason`` — why the token was revoked
    """

    approval_id: str
    execution_cursor_version: int = 0
    specification_version: int | None = None
    issued_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    expires_at: datetime
    nonce: str = Field(default_factory=lambda: __import__("secrets").token_hex(16))
    signature: str = ""
    consumed: bool = False
    # Task 3: revocation support
    revoked: bool = False
    revocation_reason: str = ""

    def canonical(self) -> str:
        """Canonical JSON string for signing (excludes ``signature`` and ``consumed``)."""
        import json as _json
        payload = self.model_dump(mode="json")
        payload.pop("signature", None)
        payload.pop("consumed", None)
        return _json.dumps(payload, sort_keys=True, separators=(",", ":"))

    def sign(self, secret: str) -> None:
        """Compute HMAC-SHA256 signature over ``canonical()``."""
        import hmac, hashlib
        self.signature = hmac.new(
            secret.encode("utf-8"),
            self.canonical().encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    def verify(self, secret: str) -> bool:
        """Verify the signature, expiry, consumption, AND revocation."""
        import hmac, hashlib
        if self.consumed:
            return False
        # Task 3: check revocation before signature validation
        if self.revoked:
            return False
        if self.expires_at < datetime.now(timezone.utc):
            return False
        expected = hmac.new(
            secret.encode("utf-8"),
            self.canonical().encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        return hmac.compare_digest(self.signature, expected)

    def consume(self) -> None:
        """Mark this token as used (single-use)."""
        self.consumed = True

    def revoke(self, reason: str = "") -> None:
        """Task 3: mark this token as revoked (invalidated)."""
        self.revoked = True
        self.revocation_reason = reason


# ---------------------------------------------------------------------------
# ResumeTokenStore - persist/hydrate ResumeToken
# ---------------------------------------------------------------------------

class ResumeTokenStore:
    """Persist ``ResumeToken`` to ``approval_requests.resume_token`` column (JSONB)."""

    @staticmethod
    async def issue(approval_id: str, token: ResumeToken) -> None:
        """Issue a new ResumeToken, first revoking any existing active token.

        This guarantees the invariant:
            "There must never be more than one active ResumeToken
             for the same approval."

        The entire rotation sequence happens atomically inside this
        method — no caller needs to revoke manually.
        """
        from core.database import get_supabase, get_supabase_async
        import json as _json
        try:
            # Step 1: load existing token (if any)
            existing = await ResumeTokenStore.load(approval_id)
            # Step 2+3: if exists and is active, revoke it
            if existing is not None and not existing.revoked and not existing.consumed:
                existing.revoke("replaced_by_new_token")
                await (await get_supabase_async()).table("approval_requests").update(
                    {"resume_token": _json.loads(existing.model_dump_json())}
                ).eq("approval_id", approval_id).execute()
                logger.info(
                    "[resume-token] revoked old token for approval %s (replaced by new)",
                    approval_id,
                )
            # Step 4+5: persist the new token
            await (await get_supabase_async()).table("approval_requests").update(
                {"resume_token": _json.loads(token.model_dump_json())}
            ).eq("approval_id", approval_id).execute()
            logger.info(
                "[resume-token] issued new token for approval %s",
                approval_id,
            )
        except Exception as exc:
            logger.error("[resume-token] failed to issue token: %s", exc)
            raise

    @staticmethod
    async def load(approval_id: str) -> ResumeToken | None:
        from core.database import get_supabase_async
        try:
            result = (
                await (await get_supabase_async())
                .table("approval_requests")
                .select("resume_token")
                .eq("approval_id", approval_id)
                .limit(1)
                .execute()
            )
            if result.data and result.data[0].get("resume_token"):
                return ResumeToken(**result.data[0]["resume_token"])
        except Exception as exc:
            logger.warning("[resume-token] failed to load token: %s", exc)
        return None

    @staticmethod
    async def consume(approval_id: str) -> None:
        """Mark the stored token as consumed (single-use)."""
        from core.database import get_supabase_async
        import json as _json
        try:
            tok = await ResumeTokenStore.load(approval_id)
            if tok is not None:
                tok.consume()
                await (await get_supabase_async()).table("approval_requests").update(
                    {"resume_token": _json.loads(tok.model_dump_json())}
                ).eq("approval_id", approval_id).execute()
        except Exception as exc:
            logger.warning("[resume-token] failed to consume token: %s", exc)

    # Task 3: revocation support

    @staticmethod
    async def revoke(approval_id: str, reason: str = "") -> None:
        """Revoke (invalidate) the stored token for ``approval_id``."""
        from core.database import get_supabase_async
        import json as _json
        try:
            tok = await ResumeTokenStore.load(approval_id)
            if tok is not None:
                tok.revoke(reason)
                await (await get_supabase_async()).table("approval_requests").update(
                    {"resume_token": _json.loads(tok.model_dump_json())}
                ).eq("approval_id", approval_id).execute()
                logger.info(
                    "[resume-token] revoked token for approval %s: %s",
                    approval_id, reason
                )
        except Exception as exc:
            logger.warning("[resume-token] failed to revoke token: %s", exc)

    @staticmethod
    async def is_revoked(approval_id: str) -> bool:
        """Check whether the stored token for ``approval_id`` is revoked."""
        try:
            tok = await ResumeTokenStore.load(approval_id)
            if tok is not None:
                return tok.revoked
        except Exception:
            pass
        return False


# ---------------------------------------------------------------------------
# Objective 6 - ApprovalPolicy
# ---------------------------------------------------------------------------

class ApprovalPolicy(str, Enum):
    """Deterministic policy for auto-approving or requiring approval."""

    AUTO = "auto"          # low-risk: auto-approve
    MANUAL = "manual"      # always require user
    REJECT = "reject"      # never allow (destructive)
    CONDITIONAL = "conditional"  # depends on context


# ---------------------------------------------------------------------------
# Objective 7 - ApprovalRule
# ---------------------------------------------------------------------------

class ApprovalRule(BaseModel):
    """A single rule for evaluating whether approval is required."""

    rule_id: str = Field(default_factory=lambda: __import__("uuid").uuid4().hex[:8])
    approval_type: ApprovalType
    policy: ApprovalPolicy
    risk_threshold: str = "medium"  # auto-approve below this
    match_pattern: str = ""  # regex or glob for file/command matching
    description: str = ""


# ---------------------------------------------------------------------------
# Objective 8 - ExecutionCursor (Objective 7 of Sprint 3)
# ---------------------------------------------------------------------------

class ExecutionCursor(BaseModel):
    """Tracks exactly where execution stopped so it can resume.

    Stored as JSONB in ``jobs.execution_cursor``.
    """

    job_id: str = ""
    cursor_version: int = 0  # optimistic locking (FIX 2)

    # Step tracking
    total_steps: int = 0
    completed_steps: list[int] = Field(default_factory=list)
    skipped_steps: list[int] = Field(default_factory=list)
    waiting_step: int | None = None
    failed_step: int | None = None
    resume_point: int = 0  # next step index to execute

    # State snapshots (for resuming)
    planner_state: dict[str, Any] = Field(default_factory=dict)
    workspace_snapshot: dict[str, Any] = Field(default_factory=dict)
    pending_steps: list[dict[str, Any]] = Field(default_factory=list)
    approval_context: dict[str, Any] = Field(default_factory=dict)

    # Timestamps
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    def mark_step_completed(self, step_index: int) -> None:
        if step_index not in self.completed_steps:
            self.completed_steps.append(step_index)
        self.resume_point = max(self.resume_point, step_index + 1)
        self.updated_at = datetime.now(timezone.utc)
        self.cursor_version += 1  # increment on every mutation

    def mark_waiting(self, step_index: int) -> None:
        self.waiting_step = step_index
        self.updated_at = datetime.now(timezone.utc)
        self.cursor_version += 1

    def clear_waiting(self) -> None:
        self.waiting_step = None
        self.updated_at = datetime.now(timezone.utc)
        self.cursor_version += 1


# ---------------------------------------------------------------------------
# Objective 9 - JobInteractionState
# ---------------------------------------------------------------------------

class JobState(str, Enum):
    """Extended job states for interactive execution."""

    RUNNING = "running"
    WAITING_FOR_USER = "waiting_for_user"
    APPROVED = "approved"
    REJECTED = "rejected"
    RESUMED = "resumed"
    COMPLETED = "completed"
    FAILED = "failed"
    PAUSED = "paused"


class InteractionMessage(BaseModel):
    """A single message in an interaction thread."""

    message_id: str = Field(default_factory=lambda: __import__("uuid").uuid4().hex[:16])
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    sender: str = "agent"  # "agent" | "user"
    message_type: str = "question"  # "question" | "answer" | "approval" | "info"
    content: str = ""
    structured: bool = False
    options: list[str] = Field(default_factory=list)  # ["YES", "NO", ...]


class JobInteractionState(BaseModel):
    """Full interaction state for a job.

    Stored as JSONB in ``jobs.interaction_state``.
    """

    job_id: str = ""
    conversation_id: str = ""
    current_state: JobState = JobState.RUNNING

    # Interaction thread
    messages: list[InteractionMessage] = Field(default_factory=list)

    # Pending approvals
    pending_approval_ids: list[str] = Field(default_factory=list)

    # Resume context
    execution_cursor: ExecutionCursor | None = None

    # Structured clarification submission (new generic form contract).
    # When the user answers a structured ClarificationForm, the authoritative
    # submission is stored here.  ``None`` for legacy free-text clarifications,
    # so old jobs keep working unchanged.
    clarification_submission: dict[str, Any] | None = None

    # Timestamps
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    waiting_since: datetime | None = None

    def add_message(self, message: InteractionMessage) -> None:
        self.messages.append(message)
        self.updated_at = datetime.now(timezone.utc)

    def transition_to(self, new_state: JobState) -> None:
        self.current_state = new_state
        self.updated_at = datetime.now(timezone.utc)
        if new_state == JobState.WAITING_FOR_USER:
            self.waiting_since = datetime.now(timezone.utc)
        elif new_state in (JobState.APPROVED, JobState.RESUMED):
            self.waiting_since = None
