"""Approval Policy Engine — Sprint 3.

Extends ``ApprovalEngine`` with configurable, deterministic policies.

Policies are evaluated BEFORE an action is executed.
If a policy requires approval, execution pauses (WAITING_FOR_USER).
"""

from __future__ import annotations

import logging
from typing import Any

from models.approval import (
    ApprovalPolicy,
    ApprovalRequest,
    ApprovalRule,
    ApprovalType,
)
from services.approval_engine import ApprovalEngine

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# ApprovalPolicyEngine
# ---------------------------------------------------------------------------


class ApprovalPolicyEngine(ApprovalEngine):
    """Configurable approval policy engine.

    Extends ``ApprovalEngine`` with:
      - Per-project policy overrides
      - Policy evaluation BEFORE action execution
      - Deterministic policy resolution (no LLM)
      """

    def __init__(
        self,
        job_id: str,
        conversation_id: str,
        *,
        project_policies: dict[str, str] | None = None,
    ) -> None:
        # Base rules (can be overridden by project_policies)
        super().__init__(job_id=job_id, conversation_id=conversation_id)
        self._project_policies = project_policies or {}

    # - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - -
    # Policy resolution (overrides)
    # - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - -

    def _resolve_policy(
        self,
        action_type: ApprovalType,
        risk_level: str,
        affected_files: list[str] | None,
    ) -> ApprovalPolicy:
        """Resolve policy with project-level overrides."""
        # 1. Check project-level override
        override_key = f"{action_type.value}:{risk_level}"
        if override_key in self._project_policies:
            try:
                return ApprovalPolicy(self._project_policies[override_key])
            except ValueError:
                logger.warning(
                    "[policy] invalid policy override: %s",
                    self._project_policies[override_key],
                )

        # 2. Fall back to base class (rule-based)
        return super()._resolve_policy(
            action_type,
            risk_level,
            affected_files,
        )

    # - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - -
    # Pre-execution hook
    # - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - -

    async def pre_execute_check(
        self,
        action_type: ApprovalType,
        title: str,
        description: str,
        *,
        risk_level: str = "medium",
        affected_files: list[str] | None = None,
        affected_commands: list[str] | None = None,
        context: dict[str, Any] | None = None,
    ) -> tuple[bool, ApprovalRequest | None]:
        """Check if an action requires approval BEFORE execution.

        Returns:
          - ``(True, None)``      → auto-approved, proceed
          - ``(False, request)`` → requires approval, pause execution
          - ``(True, request)`` → already rejected, do NOT proceed
          """
        request = await self.evaluate(
            action_type=action_type,
            title=title,
            description=description,
            risk_level=risk_level,
            affected_files=affected_files,
            affected_commands=affected_commands,
            context=context,
        )

        if request is None:
            # Auto-approved
            return True, None

        if request.status == "approved":
            return True, request

        if request.status == "rejected":
            return True, request

        # PENDING → pause execution
        return False, request

    # - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - -
    # Policy summary (for planner context)
    # - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - -

    def get_policy_summary(self) -> dict[str, Any]:
        """Return a summary of active policies (for planner context)."""
        summary: dict[str, Any] = {
            "auto_approved": [],
            "requires_approval": [],
            "rejected": [],
        }
        for rule in self.rules:
            entry = {
                "type": rule.approval_type.value,
                "risk_threshold": rule.risk_threshold,
                "policy": rule.policy.value,
            }
            if rule.policy == ApprovalPolicy.AUTO:
                summary["auto_approved"].append(entry)
            elif rule.policy == ApprovalPolicy.REJECT:
                summary["rejected"].append(entry)
            else:
                summary["requires_approval"].append(entry)
        return summary
