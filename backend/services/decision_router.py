"""Decision-driven routing authority (Sprint 4E).

In ``authoritative`` mode the Decision Engine SELECTS the execution route and the
legacy intent classification becomes a *compatibility validator*. This module
implements that transition with strict production-safety guarantees:

    Pipeline State -> Decision Engine -> Decision Graph -> Legacy Validation -> Execution

CONTRACT
--------
* ``resolve_route()`` returns an ``EffectiveRoute`` whose ``execution_kind`` the
  pipeline uses for dispatch, PLUS a full validation record (decision, legacy
  decision, agreement, conflict, reason, confidence, execution category, safety
  classification). No routing is ever hidden.

SAFETY INVARIANTS (absolute — this module cannot weaken them)
------------------------------------------------------------
* The Decision Engine CANNOT bypass Permission Engine, Write Gate, Approval,
  Workspace Isolation, Authentication, Authorization, or Ownership Validation.
  Those gates live downstream (executor/tools/permission) and run regardless of
  which route is chosen. This module only chooses among the SAME three execution
  kinds the pipeline already supports (chat/code/server); it grants no new power.
* PRIVILEGE-ESCALATION VETO: if the engine wants to route to ``server`` (the
  write/deploy path) but legacy did NOT classify ``server``, the engine is NOT
  allowed to unilaterally escalate. The route falls back to the legacy kind and
  the conflict is classified ``UNSAFE_ESCALATION_BLOCKED``. This guarantees
  authoritative mode can never grant MORE access than intent-driven mode would.
* De-escalation (engine picks a less-privileged route than legacy) is allowed
  but recorded, so a request is never silently upgraded in privilege.
* On ANY engine error the route falls back to the legacy intent (fail-safe to
  current production behavior) and is classified ``ENGINE_ERROR_FELL_BACK``.

Determinism: given the same pipeline state the resolved route and classification
are identical every call.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from services import logger as obs
from services.agent_decision_engine import (
    AgentDecisionEngine,
    DecisionState,
    ExecutionKind,
    NextAction,
)
from services.decision_shadow import _live_execution_kind

logger = logging.getLogger(__name__)

# The only routes the pipeline can dispatch. Ordered by privilege (ascending).
_PRIVILEGE = {"chat": 0, "code": 1, "server": 2}


@dataclass(frozen=True)
class EffectiveRoute:
    """The route the pipeline will execute + the full validation record."""

    execution_kind: str          # "chat" | "code" | "server" — what the pipeline dispatches
    legacy_kind: str             # what intent classification would have chosen
    decision_kind: str           # what the engine chose (may be "none" for a gate)
    agreement: bool              # decision_kind == legacy_kind
    conflict: str                # classification of any disagreement (see below)
    reason: str
    confidence: float
    execution_category: str
    safety_classification: str
    next_action: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "execution_kind": self.execution_kind,
            "legacy_kind": self.legacy_kind,
            "decision_kind": self.decision_kind,
            "agreement": self.agreement,
            "conflict": self.conflict,
            "reason": self.reason,
            "confidence": self.confidence,
            "execution_category": self.execution_category,
            "safety_classification": self.safety_classification,
            "next_action": self.next_action,
        }


# Conflict classifications (explicit — no hidden routing):
#   AGREEMENT                  decision == legacy
#   GATE_BEFORE_EXECUTION      engine advised a pre-exec gate; execution kind
#                              still taken from engine's inferred kind, legacy
#                              validates it (privilege rules apply)
#   SAFE_REROUTE               decision != legacy, no privilege escalation
#   UNSAFE_ESCALATION_BLOCKED  decision wanted MORE privilege than legacy -> vetoed
#   DEESCALATION               decision chose LESS privilege than legacy (allowed)
#   ENGINE_ERROR_FELL_BACK     engine raised -> legacy used


def resolve_route(
    *,
    job_id: str,
    trace_id: str | None,
    state: DecisionState,
) -> EffectiveRoute:
    """Select the effective execution route with legacy validation. Never raises.

    The returned ``execution_kind`` is what the pipeline dispatches on. It is
    ALWAYS one of chat/code/server and never exceeds the legacy privilege level.
    """
    legacy_kind = _live_execution_kind(state.intent)

    try:
        recommendation = AgentDecisionEngine.recommend(state)
        decision = recommendation.decision
        decision_kind_enum = decision.execution_kind
        decision_kind = decision_kind_enum.value

        # When the engine advised a pre-execution gate (execution_kind == none),
        # fall back to its inferred kind for *dispatch* purposes by re-deriving
        # from intent weighting — the gate itself is handled by the existing
        # pipeline stages. We treat the dispatch kind as the engine's inferred
        # execution kind if concrete, else the legacy kind.
        if decision_kind_enum == ExecutionKind.NONE:
            dispatch_kind = _infer_dispatch_kind(state, legacy_kind)
            gate = True
        else:
            dispatch_kind = decision_kind
            gate = False

        agreement = (dispatch_kind == legacy_kind)

        # Privilege comparison for the escalation veto.
        legacy_priv = _PRIVILEGE.get(legacy_kind, 1)
        dispatch_priv = _PRIVILEGE.get(dispatch_kind, 1)

        if agreement:
            effective = dispatch_kind
            conflict = "GATE_BEFORE_EXECUTION" if gate else "AGREEMENT"
        elif dispatch_priv > legacy_priv:
            # Engine wants MORE privilege than legacy -> VETO escalation.
            effective = legacy_kind
            conflict = "UNSAFE_ESCALATION_BLOCKED"
        elif dispatch_priv < legacy_priv:
            # Engine wants LESS privilege -> allowed, recorded.
            effective = dispatch_kind
            conflict = "DEESCALATION"
        else:
            # Same privilege, different kind (shouldn't happen with 3 distinct
            # privileges, but handle defensively) -> safe reroute.
            effective = dispatch_kind
            conflict = "SAFE_REROUTE"

        route = EffectiveRoute(
            execution_kind=effective,
            legacy_kind=legacy_kind,
            decision_kind=decision_kind,
            agreement=agreement,
            conflict=conflict,
            reason=recommendation.reason,
            confidence=recommendation.confidence,
            execution_category=recommendation.execution_category.value,
            safety_classification=recommendation.safety_level.value,
            next_action=decision.next_action.value,
        )
    except Exception as exc:  # noqa: BLE001 — authoritative path must fail safe
        logger.warning("[decision_router] engine error, falling back to legacy: %s", exc)
        route = EffectiveRoute(
            execution_kind=legacy_kind,
            legacy_kind=legacy_kind,
            decision_kind="none",
            agreement=True,
            conflict="ENGINE_ERROR_FELL_BACK",
            reason=f"engine error: {type(exc).__name__}",
            confidence=0.0,
            execution_category="gate",
            safety_classification="safe",
            next_action="execute",
        )

    # Record every routing decision — no hidden routing.
    try:
        obs.emit(
            level="INFO",
            layer="router",
            message="decision_engine_authoritative",
            trace_id=trace_id,
            meta={"job_id": job_id, "mode": "authoritative", "route": route.to_dict(),
                  "intent": state.intent},
        )
    except Exception:  # pragma: no cover — logging must never break routing
        pass

    return route


def _infer_dispatch_kind(state: DecisionState, legacy_kind: str) -> str:
    """Derive a concrete dispatch kind when the engine advised a gate.

    Mirrors the engine's own weighting (deployment signal + intent) so the
    eventual execution kind is deterministic. Never exceeds legacy privilege on
    its own — the caller still applies the escalation veto.
    """
    intent = (state.intent or "").strip().lower()
    if state.deployment_signal and intent != "code":
        return "server"
    if intent in ("chat", "code", "server"):
        return intent
    return legacy_kind


__all__ = ["resolve_route", "EffectiveRoute"]
