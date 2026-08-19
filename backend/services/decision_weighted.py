"""Decision Engine WEIGHTED harness (Sprint 4D).

Promotes the pure :class:`AgentDecisionEngine` from passive shadow observation
to a **recommend-only** layer. The engine now produces an explainable
:class:`Recommendation`; this harness runs the

    Current Decision -> Recommendation -> Agreement -> Compatibility -> Safety
    -> (Legacy Execution, unchanged)

pipeline and records a 5-way classification plus running statistics.

CRITICAL INVARIANTS (Sprint 4D):
    * The Decision Engine NEVER executes and NEVER overrides production. Legacy
      orchestration remains authoritative — this module only *records*.
    * Security invariants (Permission Engine, Write Gate, Approval, Execution
      Policy, Authorization, Workspace Isolation, Server Ownership) are NEVER
      consulted from or delegated to the engine. The ``safety_level`` label here
      is advisory metadata only; it cannot grant or bypass anything.
    * Exactly ONE effect: a structured observability log line + an in-process
      counter. No DB, no network, no filesystem, no execution. Never raises into
      the caller.

Activation: only when ``settings.decision_engine_mode == "weighted"`` (see
``agent_service.run_agent_pipeline``). Any error is swallowed.
"""

from __future__ import annotations

import logging
import threading
from typing import Any

from services import logger as obs
from services.agent_decision_engine import (
    AgentDecisionEngine,
    DecisionState,
    ExecutionCategory,
    ExecutionKind,
    NextAction,
    SafetyLevel,
)
from services.decision_shadow import _live_execution_kind

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# In-memory statistics (process-local; reset on restart). No persistence.
# ---------------------------------------------------------------------------
class _WeightedStats:
    """Thread-safe counters for recommendation classifications."""

    _CLASSES = ("MATCH", "SAFE_MISMATCH", "UNSAFE_MISMATCH", "BLOCKED", "UNKNOWN")

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counts: dict[str, int] = {c: 0 for c in self._CLASSES}
        self._total = 0
        # Stability tracking: last recommendation per job to detect flip-flops.
        self._last_by_job: dict[str, str] = {}
        self._repeated = 0
        self._flipped = 0

    def record(self, *, job_id: str, classification: str, exec_category: str) -> None:
        with self._lock:
            if classification not in self._counts:
                classification = "UNKNOWN"
            self._counts[classification] += 1
            self._total += 1
            prev = self._last_by_job.get(job_id)
            if prev is not None:
                if prev == exec_category:
                    self._repeated += 1
                else:
                    self._flipped += 1
            self._last_by_job[job_id] = exec_category

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            total = self._total
            counts = dict(self._counts)
            repeated = self._repeated
            flipped = self._flipped
        accuracy = (counts["MATCH"] / total) if total else 0.0
        unsafe_rate = (counts["UNSAFE_MISMATCH"] / total) if total else 0.0
        stability_obs = repeated + flipped
        stability = (repeated / stability_obs) if stability_obs else 1.0
        return {
            "total": total,
            "counts": counts,
            "recommendation_accuracy": round(accuracy, 4),
            "unsafe_mismatch_rate": round(unsafe_rate, 4),
            "decision_stability": round(stability, 4),
            "repeated_decisions": repeated,
            "conflict_frequency": flipped,
        }


WEIGHTED_STATS = _WeightedStats()


# ---------------------------------------------------------------------------
# Classification pipeline: Agreement -> Compatibility -> Safety
# ---------------------------------------------------------------------------
def _classify(
    *,
    live_kind: str,
    recommendation,
) -> str:
    """Return one of MATCH / SAFE_MISMATCH / UNSAFE_MISMATCH / BLOCKED / UNKNOWN.

    Pure classification of the *recommendation* against the *live* decision.
    Does not affect execution; does not consult any security gate.
    """
    decision = recommendation.decision

    # UNKNOWN: engine could not infer a concrete execution kind at an execute node.
    if decision.next_action == NextAction.EXECUTE and decision.execution_kind == ExecutionKind.NONE:
        return "UNKNOWN"

    # BLOCKED: engine recommends a pre-execution gate (clarify/discover/approve/
    # resume/plan/context) instead of executing — legacy would proceed, so the
    # recommendation is "blocked" relative to the live execute path.
    if recommendation.execution_category == ExecutionCategory.GATE:
        return "BLOCKED"

    shadow_kind = decision.execution_kind.value

    # Agreement check.
    if shadow_kind == live_kind:
        return "MATCH"

    # Compatibility + Safety check for a genuine routing disagreement.
    # SENSITIVE (server/deploy) disagreements are UNSAFE — recommending a write
    # path the live pipeline did NOT choose (or vice-versa) must be flagged.
    if recommendation.safety_level == SafetyLevel.SENSITIVE:
        return "UNSAFE_MISMATCH"
    if "server" in (shadow_kind, live_kind):
        return "UNSAFE_MISMATCH"

    # chat<->code disagreement carries no write implication -> SAFE.
    return "SAFE_MISMATCH"


def record_weighted_recommendation(
    *,
    job_id: str,
    trace_id: str | None,
    state: DecisionState,
) -> dict[str, Any] | None:
    """Compute a weighted recommendation, classify it, record it. Never raises.

    Returns the observation payload (for tests) or ``None`` on internal error.
    Execution is NOT affected; legacy orchestration remains authoritative.
    """
    try:
        recommendation = AgentDecisionEngine.recommend(state)
        live_kind = _live_execution_kind(state.intent)
        classification = _classify(live_kind=live_kind, recommendation=recommendation)

        WEIGHTED_STATS.record(
            job_id=job_id,
            classification=classification,
            exec_category=recommendation.execution_category.value,
        )

        observation: dict[str, Any] = {
            "job_id": job_id,
            "mode": "weighted",
            "classification": classification,
            "live_execution_kind": live_kind,
            "recommendation": recommendation.to_dict(),
            "stats": WEIGHTED_STATS.snapshot(),
        }

        obs.emit(
            level="INFO",
            layer="router",
            message="decision_engine_weighted",
            trace_id=trace_id,
            meta=observation,
        )
        return observation
    except Exception as exc:  # noqa: BLE001 — recommend path must never break prod
        logger.warning("[decision_weighted] recommendation failed (non-fatal): %s", exc)
        return None


__all__ = ["record_weighted_recommendation", "WEIGHTED_STATS"]
