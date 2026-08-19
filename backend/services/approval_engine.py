"""Approval Engine — Sprint 3: Human-in-the-Loop Orchestration.

Evaluates every action against deterministic approval policies.
Creates approval requests that block execution until the user decides.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from models.approval import (
    ApprovalAuditEvent,
    ApprovalDecision,
    ApprovalPolicy,
    ApprovalRequest,
    ApprovalRule,
    ApprovalStatus,
    ApprovalType,
)
from models.job import JobStatus
from services.conversation_reliability import OptimisticLockError

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Default approval rules (can be overridden per project)
# ---------------------------------------------------------------------------

_DEFAULT_RULES: list[ApprovalRule] = [
    # Low-risk: auto-approve
    ApprovalRule(
        approval_type=ApprovalType.FILE_OVERWRITE,
        policy=ApprovalPolicy.AUTO,
        risk_threshold="low",
        description="Auto-approve low-risk file overwrites (non-sensitive paths)",
    ),
    ApprovalRule(
        approval_type=ApprovalType.PATCH,
        policy=ApprovalPolicy.AUTO,
        risk_threshold="low",
        description="Auto-approve small patches",
    ),
    # Medium-risk: manual approval
    ApprovalRule(
        approval_type=ApprovalType.COMMAND,
        policy=ApprovalPolicy.MANUAL,
        risk_threshold="medium",
        description="Require approval for shell commands",
    ),
    ApprovalRule(
        approval_type=ApprovalType.ASSUMPTION,
        policy=ApprovalPolicy.MANUAL,
        risk_threshold="medium",
        description="Require approval for critical assumptions",
    ),
    # High-risk: manual + explicit reason
    ApprovalRule(
        approval_type=ApprovalType.DEPLOYMENT,
        policy=ApprovalPolicy.MANUAL,
        risk_threshold="high",
        description="Require approval for deployments",
    ),
    ApprovalRule(
        approval_type=ApprovalType.REQUIREMENT,
        policy=ApprovalPolicy.MANUAL,
        risk_threshold="high",
        description="Require approval for requirement changes",
    ),
    # Critical: never auto-approve
    ApprovalRule(
        approval_type=ApprovalType.DESTRUCTIVE,
        policy=ApprovalPolicy.MANUAL,
        risk_threshold="critical",
        description="All destructive operations require approval",
    ),
    ApprovalRule(
        approval_type=ApprovalType.SECRET,
        policy=ApprovalPolicy.MANUAL,
        risk_threshold="critical",
        description="Secret generation requires approval",
    ),
]


# ---------------------------------------------------------------------------
# ApprovalEngine
# ---------------------------------------------------------------------------

class ApprovalEngine:
    """Deterministic approval evaluation engine.

    Responsibilities:
      - Evaluate actions against approval rules
      - Create ``ApprovalRequest`` for actions requiring approval
      - Persist audit events
      - Support resumable approval (checkpoint approval state)
    """

    def __init__(
        self,
        job_id: str,
        conversation_id: str,
        rules: list[ApprovalRule] | None = None,
    ) -> None:
        self.job_id = job_id
        self.conversation_id = conversation_id
        self.rules = rules or list(_DEFAULT_RULES)

    # - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - -
    # Public API
    # - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - -

    async def evaluate(
        self,
        action_type: ApprovalType,
        title: str,
        description: str,
        *,
        risk_level: str = "medium",
        affected_files: list[str] | None = None,
        affected_commands: list[str] | None = None,
        affected_assumptions: list[str] | None = None,
        context: dict[str, Any] | None = None,
    ) -> ApprovalRequest | None:
        """Evaluate an action.  Returns ``None`` if no approval is required.

        Returns an ``ApprovalRequest`` if the action requires user confirmation.
        """
        policy = self._resolve_policy(action_type, risk_level, affected_files)

        if policy == ApprovalPolicy.AUTO:
            logger.info(
                "[approval] auto-approved: type=%s title=%s",
                action_type.value,
                title,
            )
            return None

        if policy == ApprovalPolicy.REJECT:
            logger.warning(
                "[approval] rejected by policy: type=%s title=%s",
                action_type.value,
                title,
            )
            # Create a rejected approval request (never blocks)
            request = ApprovalRequest(
                job_id=self.job_id,
                conversation_id=self.conversation_id,
                approval_type=action_type,
                status=ApprovalStatus.REJECTED,
                title=title,
                description=description,
                risk_level=risk_level,
                affected_files=affected_files or [],
                affected_commands=affected_commands or [],
                affected_assumptions=affected_assumptions or [],
                context=context or {},
                resolved_at=datetime.now(timezone.utc),
                resolved_by="system",
                decision=ApprovalDecision.REJECTED,
                reason="Rejected by approval policy (REJECT)",
            )
            await self._audit(request, "rejected", reason="Policy=REJECT")
            return request  # returned but not blocking

        # MANUAL or CONDITIONAL → create pending approval request
        request = ApprovalRequest(
            job_id=self.job_id,
            conversation_id=self.conversation_id,
            approval_type=action_type,
            status=ApprovalStatus.PENDING,
            title=title,
            description=description,
            risk_level=risk_level,
            affected_files=affected_files or [],
            affected_commands=affected_commands or [],
            affected_assumptions=affected_assumptions or [],
            context=context or {},
        )
        await self._persist(request)
        await self._audit(request, "created")
        logger.info(
            "[approval] pending approval: approval_id=%s type=%s title=%s",
            request.approval_id,
            action_type.value,
            title,
        )
        return request

    async def resolve(
        self,
        approval_id: str,
        decision: ApprovalDecision,
        *,
        reason: str = "",
        user: str = "user",
    ) -> ApprovalRequest:
        """Resolve a pending approval request.

        Uses shared reliability layer (Objectives 1, 2, 4).
        """
        # Idempotency check (Objective 1)
        operation_id = IdempotencyGuard.generate_operation_id(
            self.job_id, f"resolve:{approval_id}:{decision.value}",
        )
        previous = await IdempotencyGuard.check(self.job_id, operation_id)
        if previous is not None:
            raise IdempotencyError(operation_id)

        request = await self._load(approval_id)
        if request.status != ApprovalStatus.PENDING:
            raise ValueError(
                f"Approval {approval_id} is already {request.status.value}"
            )

        request.status = (
            ApprovalStatus.APPROVED
            if decision == ApprovalDecision.APPROVED
            else ApprovalStatus.REJECTED
            if decision == ApprovalDecision.REJECTED
            else ApprovalStatus.SKIPPED
        )
        request.decision = decision
        request.reason = reason
        request.resolved_by = user
        request.resolved_at = datetime.now(timezone.utc)

        # Atomic persistence with optimistic locking (Objectives 2, 3)
        await OptimisticLockGuard.save_approval_atomic(
            request,
            expected_version=getattr(request, "request_version", None),
        )
        await self._audit(
            request,
            "approved" if decision == ApprovalDecision.APPROVED else "rejected",
            decision=decision,
            reason=reason,
            user=user,
        )

        # Record idempotency (Objective 1)
        await IdempotencyGuard.record(
            self.job_id, self.conversation_id,
            operation_id=operation_id,
            result={"approval_id": approval_id, "decision": decision.value},
        )

        # Auto-revoke token on rejection (Sprint 3A.3)
        if decision == ApprovalDecision.REJECTED:
            await _TokenRevocationEngine.revoke_for_approval(
                approval_id, reason=reason or "User rejected",
            )

        return request

    # - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - -
    # Policy resolution
    # - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - -

    def _resolve_policy(
        self,
        action_type: ApprovalType,
        risk_level: str,
        affected_files: list[str] | None,
    ) -> ApprovalPolicy:
        """Determine the effective policy for an action."""
        for rule in self.rules:
            if rule.approval_type != action_type:
                continue
            # Simple risk-based evaluation
            risk_order = {"low": 0, "medium": 1, "high": 2, "critical": 3}
            threshold = risk_order.get(rule.risk_threshold, 1)
            actual = risk_order.get(risk_level, 1)
            if actual <= threshold and rule.policy == ApprovalPolicy.AUTO:
                return ApprovalPolicy.AUTO
            return rule.policy
        # Default: manual approval for unknown types
        return ApprovalPolicy.MANUAL

    # - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - -
    # Persistence (Supabase)
    # - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - -

    async def _persist(self, request: ApprovalRequest) -> None:
        """Persist approval request to ``approval_requests`` table.

        Uses optimistic locking on ``request_version`` (Task 1 — Sprint 3B.2.1).
        Increments ``request_version`` on every write.
        Raises ``OptimisticLockError`` on version mismatch.
        """
        from core.database import get_supabase, get_supabase_async
        import json as _json

        # Decide CREATE vs UPDATE BEFORE any DB access, from repository state.
        # A brand-new ApprovalRequest starts at request_version == 0 (model
        # default); an existing row loaded via _load() carries version >= 1.
        is_create = (request.request_version or 0) == 0

        # Increment version (_persist is the sole owner of these increments)
        request.request_version = (request.request_version or 0) + 1
        request.updated_at = datetime.now(timezone.utc)

        data = _json.loads(request.model_dump_json())

        try:
            if is_create:
                # CREATE path — true INSERT (never UPDATE-first, never UPSERT).
                result = (
                    await (await get_supabase_async())
                    .table("approval_requests")
                    .insert(data)
                    .execute()
                )
                if not result.data:
                    raise RuntimeError(
                        f"Failed to insert approval {request.approval_id}"
                    )
                return

            # UPDATE path — optimistic locking: only update if DB version matches
            result = (
                await (await get_supabase_async())
                .table("approval_requests")
                .update(data)
                .eq("approval_id", request.approval_id)
                .eq("request_version", request.request_version - 1)
                .execute()
            )
            if not result.data:
                # 0 rows updated. Re-read the ACTUAL stored version to decide
                # whether this is a genuine version conflict. Raising on an empty
                # result alone is wrong: a missing/deleted row is not a version
                # conflict, and reporting `expected == actual` is impossible.
                current = await (
                    (await get_supabase_async())
                    .table("approval_requests")
                    .select("request_version")
                    .eq("approval_id", request.approval_id)
                    .limit(1)
                    .execute()
                )
                expected_version = request.request_version - 1
                actual_version = (
                    current.data[0]["request_version"] if current.data else None
                )
                if actual_version != expected_version:
                    raise OptimisticLockError(
                        expected=expected_version,
                        actual=actual_version,
                    )
                # Row is present at the expected version but the UPDATE affected
                # 0 rows (e.g. row missing/deleted) — NOT a version conflict.
                raise ValueError(
                    f"Approval {request.approval_id} not found for update "
                    f"(expected version {expected_version})"
                )
        except OptimisticLockError:
            raise
        except Exception as exc:
            logger.error("[approval] failed to persist approval: %s", exc)
            raise

    async def _load(self, approval_id: str) -> ApprovalRequest:
        """Load an approval request from Supabase."""
        from core.database import get_supabase_async

        result = (
            await (await get_supabase_async())
            .table("approval_requests")
            .select("*")
            .eq("approval_id", approval_id)
            .limit(1)
            .execute()
        )
        if not result.data:
            raise ValueError(f"Approval {approval_id} not found")
        return ApprovalRequest(**result.data[0])

    async def _audit(
        self,
        request: ApprovalRequest,
        event_type: str,
        *,
        decision: ApprovalDecision | None = None,
        reason: str = "",
        user: str = "",
    ) -> None:
        """Append an audit event to ``approval_audit`` table."""
        from core.database import get_supabase_async

        event = ApprovalAuditEvent(
            approval_id=request.approval_id,
            job_id=self.job_id,
            conversation_id=self.conversation_id,
            event_type=event_type,
            decision=decision,
            reason=reason,
            user=user,
        )
        try:
            await (await get_supabase_async()).table("approval_audit").insert(
                event.model_dump(mode="json")
            ).execute()
        except Exception as exc:
            logger.warning("[approval] audit write failed: %s", exc)
