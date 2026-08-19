"""Decision Engine SHADOW harness (Sprint 4C).

This is the *observation* layer that runs the pure :class:`AgentDecisionEngine`
in parallel with the live pipeline and records a MATCH / MISMATCH comparison.

It is deliberately kept OUT of ``agent_decision_engine`` so the engine itself
stays 100% pure. This harness is allowed exactly ONE kind of effect: emitting a
structured observability log line (the "record" required by the brief). It does
NOT touch the database, Redis, the network, the filesystem, or execution, and it
NEVER raises into the caller — any internal error is swallowed and logged so the
shadow path can never affect production.

Activation is guarded by the ``DECISION_ENGINE_SHADOW`` feature flag. When the
flag is off the harness is never called (see ``agent_service.run_agent_pipeline``).
"""

from __future__ import annotations

import logging
from typing import Any

from services import logger as obs
from services.agent_decision_engine import (
    AgentDecisionEngine,
    DecisionState,
    ExecutionKind,
)

logger = logging.getLogger(__name__)


def _live_execution_kind(intent: str | None) -> str:
    """Map the CURRENT pipeline's classified intent to its execution kind.

    The live pipeline dispatches on ``intent`` (chat/code/server). This is the
    authoritative value we compare the shadow decision against.
    """
    normalized = (intent or "").strip().lower()
    if normalized in ("chat", "code", "server"):
        return normalized
    # The pipeline coerces anything else to "code" (agent_service.py:1519).
    return "code"


def record_shadow_comparison(
    *,
    job_id: str,
    trace_id: str | None,
    state: DecisionState,
) -> dict[str, Any] | None:
    """Compute the shadow decision and record MATCH/MISMATCH. Never raises.

    Returns the observation payload (for tests) or ``None`` on internal error.
    Execution is NOT affected in any way.
    """
    try:
        decision = AgentDecisionEngine.decide(state)

        live_kind = _live_execution_kind(state.intent)
        shadow_kind = decision.execution_kind.value

        # NONE means the engine advised a pre-execution gate (clarify/discover/
        # approve/resume) rather than an execution kind; it is not a routing
        # disagreement, so it is recorded as a distinct outcome.
        if decision.execution_kind == ExecutionKind.NONE:
            outcome = "GATE"
        elif shadow_kind == live_kind:
            outcome = "MATCH"
        else:
            outcome = "MISMATCH"

        observation: dict[str, Any] = {
            "job_id": job_id,
            "outcome": outcome,
            "live_execution_kind": live_kind,
            "shadow_execution_kind": shadow_kind,
            "shadow_next_action": decision.next_action.value,
            "shadow_reason": decision.reason,
            "shadow_confidence": decision.confidence,
            "shadow_required_modules": list(decision.required_modules),
            "intent": state.intent,
            "intent_confidence": state.intent_confidence,
        }

        obs.emit(
            level="INFO",
            layer="router",
            message="decision_engine_shadow",
            trace_id=trace_id,
            meta=observation,
        )
        return observation
    except Exception as exc:  # noqa: BLE001 — shadow path must never break prod
        logger.warning("[decision_shadow] comparison failed (non-fatal): %s", exc)
        return None


__all__ = ["record_shadow_comparison"]
