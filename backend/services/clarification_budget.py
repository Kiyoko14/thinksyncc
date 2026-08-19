"""
Clarification Budget — Sprint 3F1 (Production Readiness, PART 2).

EXTENSION ONLY. Caps the number of clarification questions per task so the
agent never asks unlimited questions.

Before asking, the budget engine checks whether the answer already exists in:
  - Workspace (repository index / workspace awareness)
  - Conversation history
  - Project Brain
  - Session Snapshot
  - Specification
  - Decision Memory
  - Architecture Memory
  - Repository Index

Decision rules:
  - If the answer already exists -> do NOT ask.
  - If assumptions are safe -> continue automatically.
  - If assumptions are dangerous (blocking/high-risk) -> ask.
  - If the budget is exhausted -> prefer safe continuation; only interrupt the
    user when absolutely necessary (blocking/high-risk gaps).

This module is intentionally framework-free and pure (no I/O); the orchestrator
injects already-gathered knowledge so it can be unit-tested and reused by the
existing ``AdaptiveClarificationEngine`` without duplication.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Budget decision
# --------------------------------------------------------------------------- #


class BudgetVerdict(str, Enum):
    ASK = "ask"
    SKIP = "skip"          # answer already known -> do not ask
    SAFE_ASSUME = "safe_assume"
    EXHAUSTED = "exhausted"  # budget spent -> only blocking/high-risk may interrupt


@dataclass
class ClarificationBudget:
    """Tracks remaining clarification questions for a single task/session.

    ``max_questions`` bounds the total interruptions; ``min_remaining_for_blocking``
    is reserved so that, even when the budget is nearly spent, a genuinely
    blocking/high-risk gap can still interrupt the user (absolute necessity).
    """

    max_questions: int = 3
    # A small reserve is always kept for blocking/high-risk gaps.
    reserve_for_blocking: int = 1
    asked: list[str] = field(default_factory=list)

    @property
    def remaining(self) -> int:
        return max(0, self.max_questions - len(self.asked))

    @property
    def remaining_for_blocking(self) -> int:
        return max(0, self.remaining - (self.max_questions - self.reserve_for_blocking - len(self.asked)))

    def can_ask(self, *, blocking: bool = False) -> bool:
        if blocking:
            return self.remaining_for_blocking > 0
        return self.remaining > 0

    def record(self, question_text: str) -> None:
        self.asked.append(question_text)


@dataclass
class BudgetContext:
    """The knowledge already available — used to avoid re-asking."""

    workspace_knowledge: str = ""
    conversation_history: list[dict[str, Any]] = field(default_factory=list)
    project_brain: str = ""
    session_snapshot: str = ""
    specification: str = ""
    decision_memory: str = ""
    architecture_memory: str = ""
    repository_index: str = ""

    def _haystack(self) -> str:
        return "\n".join(
            str(x) for x in (
                self.workspace_knowledge,
                self.conversation_history,
                self.project_brain,
                self.session_snapshot,
                self.specification,
                self.decision_memory,
                self.architecture_memory,
                self.repository_index,
            )
        )


# --------------------------------------------------------------------------- #
# Budget engine
# --------------------------------------------------------------------------- #


class ClarificationBudgetEngine:
    """Pure decision engine: should we ask, skip, safe-assume, or is exhausted?"""

    # Keywords that signal a question is genuinely safe to assume.
    SAFE_TOKENS = {"default", "optional", "preference", "style", "naming", "convention", "cosmetic"}

    @staticmethod
    def _normalize(text: str) -> str:
        return " ".join((text or "").lower().split())

    @classmethod
    def answer_known(cls, question_text: str, ctx: BudgetContext) -> bool:
        """True if the existing knowledge already answers this question."""
        q = cls._normalize(question_text)
        if not q:
            return False
        hay = cls._normalize(ctx._haystack())
        if not hay:
            return False
        # If the question's key tokens appear in existing knowledge, skip it.
        tokens = [t for t in q.split() if len(t) >= 4]
        if not tokens:
            # Very short question — require a strong substring match.
            return q in hay
        return any(tok in hay for tok in tokens)

    @classmethod
    def evaluate(
        cls,
        *,
        question_text: str,
        blocking: bool,
        risk_level: str,  # "critical" | "high" | "medium" | "low"
        ctx: BudgetContext,
        budget: ClarificationBudget,
    ) -> BudgetVerdict:
        """Decide whether a candidate question should be asked."""
        # 1. Answer already exists -> never ask.
        if cls.answer_known(question_text, ctx):
            return BudgetVerdict.SKIP

        # 2. Budget check.
        if not budget.can_ask(blocking=blocking):
            # Out of budget. Only blocking/high-risk may still interrupt.
            if blocking or risk_level in {"critical", "high"}:
                if budget.remaining_for_blocking > 0:
                    return BudgetVerdict.ASK
                # Absolutely necessary? The brief says only interrupt when
                # absolutely necessary — a blocking gap with zero reserve still
                # must surface, but we mark it exhausted so the orchestrator
                # can decide. We still ASK because blocking gaps cannot be
                # safely assumed.
                return BudgetVerdict.ASK
            # Non-blocking, over budget -> safe continuation preferred.
            return BudgetVerdict.EXHAUSTED

        # 3. Within budget: blocking/high-risk always ask; otherwise safe-assume
        #    if the question is low-value/cosmetic, else ask.
        if blocking or risk_level in {"critical", "high"}:
            return BudgetVerdict.ASK
        if risk_level in {"medium", "low"} and cls._is_safe_to_assume(question_text):
            return BudgetVerdict.SAFE_ASSUME
        return BudgetVerdict.ASK

    @classmethod
    def _is_safe_to_assume(cls, question_text: str) -> bool:
        low = cls._normalize(question_text)
        if any(tok in low for tok in cls.SAFE_TOKENS):
            return True
        # Questions phrased as preferences/options are safe to assume.
        if low.startswith(("what is your", "do you prefer", "would you like", "any preference")):
            return True
        return False


__all__ = [
    "ClarificationBudget",
    "ClarificationBudgetEngine",
    "BudgetContext",
    "BudgetVerdict",
]
