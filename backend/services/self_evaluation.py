"""
Self Evaluation — Sprint 3F1 (Production Readiness, PART 6).

EXTENSION ONLY. Before every major planning step the agent internally
evaluates whether it already has enough knowledge to proceed safely.

The evaluation is INTERNAL ONLY. Its verdict must never be exposed to the user.
It returns a ``SelfEvaluation`` with a ``proceed`` flag and a recommended
action (continue / inspect_file / ask_user / refresh_memory) so the
orchestrator can act without leaking the reasoning to the user surface.

This reuses the existing confidence/freshness signals (``ConfidenceEngine``,
``Freshness``) and the existing workspace index — no new state, no duplication.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Any

from services.context_memory import ConfidenceEngine, Freshness
from services.project_brain import Confidence, KnowledgeItem

logger = logging.getLogger(__name__)


class EvalAction(str, Enum):
    CONTINUE = "continue"          # safe to proceed with current knowledge
    INSPECT_FILE = "inspect_file"  # one more file would resolve uncertainty
    ASK_USER = "ask_user"          # must clarify a blocking gap
    REFRESH_MEMORY = "refresh_memory"  # reload stale/low-confidence knowledge


@dataclass
class SelfEvaluation:
    proceed: bool
    action: EvalAction
    confidence: Confidence
    # Internal reasoning — MUST NOT be shown to the user.
    reasoning: str = ""

    def to_internal_dict(self) -> dict[str, Any]:
        return {
            "proceed": self.proceed,
            "action": self.action.value,
            "confidence": self.confidence.value,
            "reasoning": self.reasoning,
        }


class SelfEvaluator:
    """Internal pre-planning knowledge sufficiency check."""

    def __init__(self, *, low_confidence_threshold: Confidence = Confidence.LOW) -> None:
        self._threshold = low_confidence_threshold

    def evaluate(
        self,
        *,
        task: str,
        confidence: Confidence,
        knowledge_items: list[KnowledgeItem] | None = None,
        missing_required: bool = False,      # a blocking gap exists
        ambiguous_enough_to_ask: bool = False,
        can_inspect_more: bool = True,       # more files are available
    ) -> SelfEvaluation:
        """Answer the 6 internal questions and produce a verdict.

        Internal questions (never surfaced):
          1. Do I already have enough knowledge?
          2. Can I continue safely?
          3. Should I inspect another file?
          4. Should I ask the user?
          5. Should I trust existing memory?
          6. Should I refresh knowledge?
        """
        items = knowledge_items or []
        stale = any(Freshness.is_stale(i) for i in items)
        must_reload = any(ConfidenceEngine.should_reload(i) for i in items)

        # Q5/Q6: trust existing memory? refresh knowledge?
        trust_memory = not (stale or must_reload)
        refresh_knowledge = stale or must_reload

        # Q2: can I continue safely? Not if a blocking gap or if confidence
        #     is below the reload threshold.
        can_continue_safely = (not missing_required) and (not confidence.below(self._threshold))

        # Q3: should I inspect another file? Yes, if uncertain but more is available.
        should_inspect = (not can_continue_safely) and can_inspect_more and (not missing_required)

        # Q4: should I ask the user? Only for blocking/ambiguous gaps that
        #     cannot be resolved by inspection.
        should_ask = missing_required or (ambiguous_enough_to_ask and not can_inspect_more)

        # Q1: enough knowledge?
        enough = can_continue_safely and trust_memory

        # Decide action.
        if should_ask:
            action = EvalAction.ASK_USER
            proceed = False
            reasoning = "blocking/ambiguous gap cannot be resolved by inspection"
        elif refresh_knowledge and not enough:
            action = EvalAction.REFRESH_MEMORY
            proceed = False
            reasoning = "stale or low-confidence knowledge requires reload of affected scope"
        elif should_inspect:
            action = EvalAction.INSPECT_FILE
            proceed = False
            reasoning = "inspecting one more relevant file resolves uncertainty"
        elif enough:
            action = EvalAction.CONTINUE
            proceed = True
            reasoning = "sufficient, fresh, confident knowledge; safe to proceed"
        else:
            action = EvalAction.REFRESH_MEMORY
            proceed = False
            reasoning = "insufficient knowledge and no safe path forward without refresh"

        return SelfEvaluation(
            proceed=proceed,
            action=action,
            confidence=confidence,
            reasoning=reasoning,
        )


__all__ = ["SelfEvaluator", "SelfEvaluation", "EvalAction"]
