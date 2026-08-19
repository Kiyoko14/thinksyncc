"""
Knowledge Consistency — Sprint 3F1 (Production Readiness, PART 4).

EXTENSION ONLY. Ensures the long-term engineering memory never holds two
actively-conflicting facts.

When new engineering knowledge contradicts previous knowledge:
  1. Mark the *previous* knowledge obsolete (do not delete — preserve history).
  2. Update Project Brain (THINKSYNC.md) via the Context Diff Engine.
  3. Update Decision Memory (the conflicting decision is superseded).
  4. Update Architecture Memory (if the conflict was architectural).

The module is pure (no I/O): a ``KnowledgeConsistency`` instance classifies a
new fact against the existing set and returns a ``ConsistencyResult``; the
caller applies the writes through the existing ``ProjectBrain`` /
``DecisionMemory`` / ``ArchitectureMemory`` services so no second persistence
layer is introduced.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Any

logger = logging.getLogger(__name__)


class ConsistencyAction(str, Enum):
    ACCEPT = "accept"            # new fact is consistent -> store as-is
    OBSOLETE_PREVIOUS = "obsolete_previous"  # previous fact marked obsolete
    CONFLICT = "conflict"        # unresolvable conflict -> flag for review


@dataclass
class KnowledgeFact:
    """A single engineering fact with enough metadata to detect conflicts."""

    key: str
    value: str
    category: str = "general"    # e.g. "decision", "architecture", "tech_stack"
    confidence: str = "medium"   # high | medium | low
    source: str = "agent"
    obsolete: bool = False

    def is_contradictory_with(self, other: "KnowledgeFact") -> bool:
        """Two facts conflict when they share a key but assert different values."""
        if self.key != other.key:
            return False
        if self.obsolete or other.obsolete:
            return False
        a = self._norm(self.value)
        b = self._norm(other.value)
        if not a or not b:
            return False
        # Exact-match value -> not a contradiction.
        if a == b:
            return False
        # Same key, different (non-empty) values -> the facts conflict.
        return True

    @staticmethod
    def _norm(text: str) -> str:
        return " ".join((text or "").lower().split())


@dataclass
class ConsistencyResult:
    action: ConsistencyAction
    # The previous fact that should be marked obsolete (if any).
    obsolete_previous: KnowledgeFact | None = None
    # Human-internal note (never user-facing).
    note: str = ""


class KnowledgeConsistency:
    """Classify a new fact against an existing active set."""

    @classmethod
    def check(
        cls,
        *,
        new_fact: KnowledgeFact,
        existing: list[KnowledgeFact],
    ) -> ConsistencyResult:
        """Return the consistency action for ``new_fact``."""
        for prev in existing:
            if prev.obsolete:
                continue
            if prev.key == new_fact.key and prev.value == new_fact.value:
                # Identical -> already known, accept (no-op).
                return ConsistencyResult(
                    action=ConsistencyAction.ACCEPT,
                    note="identical to existing active fact",
                )
            if prev.is_contradictory_with(new_fact):
                # Prefer the newer / higher-confidence fact.
                keep_new = cls._prefer(new_fact, prev)
                if keep_new:
                    return ConsistencyResult(
                        action=ConsistencyAction.OBSOLETE_PREVIOUS,
                        obsolete_previous=prev,
                        note=f"superseded by newer fact (source={new_fact.source})",
                    )
                return ConsistencyResult(
                    action=ConsistencyAction.CONFLICT,
                    obsolete_previous=prev,
                    note="lower-confidence new fact conflicts with active higher-confidence fact",
                )
        return ConsistencyResult(action=ConsistencyAction.ACCEPT)

    @staticmethod
    def _prefer(new: KnowledgeFact, prev: KnowledgeFact) -> bool:
        rank = {"high": 3, "medium": 2, "low": 1}
        rn = rank.get(new.confidence, 2)
        rp = rank.get(prev.confidence, 2)
        if rn != rp:
            return rn > rp
        # Same confidence -> newer source wins (agent > older auto).
        return new.source != "legacy"

    # -- application helpers (reuse existing services) -------------------- #

    @staticmethod
    async def apply(
        *,
        brain: Any,
        decision_memory: Any,
        architecture_memory: Any,
        new_fact: KnowledgeFact,
        result: ConsistencyResult,
    ) -> None:
        """Persist a consistency decision using existing memory services.

        Never introduces a parallel store — all writes go through
        ``ProjectBrain`` / ``DecisionMemory`` / ``ArchitectureMemory``.
        """
        if result.action is ConsistencyAction.ACCEPT:
            await brain.record_change(
                change_type=f"consistency:{new_fact.category}",
                description=f"- {new_fact.key}: {new_fact.value}",
            )
            return

        if result.obsolete_previous is not None:
            prev = result.obsolete_previous
            # Mark previous obsolete in the Project Brain (THINKSYNC.md) via the
            # diff engine (preserves manual notes, minimal change).
            try:
                await brain.record_change(
                    change_type="knowledge_obsoleted",
                    description=(
                        f"- ~~{prev.key}: {prev.value}~~ _(obsoleted by: "
                        f"{new_fact.key}: {new_fact.value})_"
                    ),
                )
            except Exception as exc:  # noqa: BLE001 — best-effort, never block
                logger.warning("[consistency] brain update failed: %s", exc)

            # Update Decision Memory if it was a decision.
            if new_fact.category == "decision" and decision_memory is not None:
                try:
                    await decision_memory.record(
                        title=new_fact.key,
                        rationale=f"updated: {new_fact.value}",
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning("[consistency] decision update failed: %s", exc)

            # Update Architecture Memory if it was architectural.
            if new_fact.category == "architecture" and architecture_memory is not None:
                try:
                    from services.project_brain import Confidence
                    from services.context_memory import ArchitectureNode

                    architecture_memory.register(
                        node=ArchitectureNode(
                            name=new_fact.key,
                            kind="component",
                            description=new_fact.value[:120],
                            confidence=Confidence.MEDIUM,
                        )
                    )
                    await architecture_memory.sync_to_brain()
                except Exception as exc:  # noqa: BLE001
                    logger.warning("[consistency] architecture update failed: %s", exc)


__all__ = [
    "KnowledgeConsistency",
    "KnowledgeFact",
    "ConsistencyResult",
    "ConsistencyAction",
]
