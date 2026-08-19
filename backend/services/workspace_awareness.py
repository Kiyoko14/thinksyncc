"""
Workspace Awareness — Sprint 3F1 (Production Readiness, PART 1).

EXTENSION ONLY. Improves the agent's understanding of the *actual* workspace
without rescanning the entire repository.

Design:
  • Reuses the existing ``RepositoryIndex`` (which is itself built on the
    existing ``ContextEngine._index_workspace_files`` and the ``workspace_files``
    Supabase table — no parallel index is introduced).
  • Only inspects additional files when confidence in the current workspace
    understanding is insufficient (gated by ``ConfidenceEngine``).
  • When new workspace knowledge is learned, it feeds back into the Project
    Brain (THINKSYNC.md) automatically so long-term memory improves over time.
  • Never increases token usage unnecessarily: the index is incremental and
    every extra file read is bounded and counted.

Production weaknesses addressed (from the audit):
  - Repository awareness was spec-derived; the agent now reads the live
    ``workspace_files`` index and the incremental change set.
  - Workspace understanding did not flow back into the Project Brain.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from services.context_memory import ConfidenceEngine
from services.project_brain import Confidence, KnowledgeItem
from services.repository_index import RepositoryIndex

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Workspace Awareness
# --------------------------------------------------------------------------- #


@dataclass
class WorkspaceUnderstanding:
    """A snapshot of what the agent currently knows about the workspace."""

    workspace_id: str = ""
    entry_points: list[str] = field(default_factory=list)
    services: list[str] = field(default_factory=list)
    db_models: list[str] = field(default_factory=list)
    responsibilities: list[str] = field(default_factory=list)
    changed_files: list[str] = field(default_factory=list)
    scanned_full: bool = False
    confidence: Confidence = Confidence.MEDIUM

    def to_context_block(self) -> str:
        lines = ["Workspace understanding (incremental):"]
        if self.entry_points:
            lines.append("Entry points: " + ", ".join(self.entry_points))
        if self.services:
            lines.append("Services: " + ", ".join(self.services))
        if self.db_models:
            lines.append("DB models: " + ", ".join(self.db_models))
        if self.responsibilities:
            lines.append("Responsibilities: " + "; ".join(self.responsibilities[:10]))
        if self.changed_files:
            lines.append("Recent changes: " + ", ".join(self.changed_files[:15]))
        if self.scanned_full:
            lines.append("(full scan performed)")
        return "\n".join(lines)


class WorkspaceAwareness:
    """Build and maintain workspace understanding from the existing index.

    Uses ``ConfidenceEngine.compute`` to decide whether to stop (sufficient
    knowledge) or escalate to additional inspection.
    """

    def __init__(self, index: RepositoryIndex | None = None) -> None:
        self._index = index or RepositoryIndex()
        # Signals persisted across calls within a single session so confidence
        # can rise after successful verification.
        self._verified: set[str] = set()
        self._last_confidence = Confidence.MEDIUM

    async def understand(
        self,
        *,
        workspace_id: str,
        server: dict[str, Any],
        workspace_path: str,
        task: str = "",
        # Optional injected context (never re-derived if provided).
        conversation_history: list[dict[str, Any]] | None = None,
        specification_text: str = "",
        architecture_text: str = "",
        decision_text: str = "",
        force_inspect: bool = False,
    ) -> WorkspaceUnderstanding:
        """Produce a workspace understanding, escalating only when needed.

        Progressive: refreshes the incremental index (already cheap), derives a
        confidence, and only triggers deeper inspection if confidence is below
        the reload threshold OR ``force_inspect`` is set.
        """
        summary = await self._index.refresh(
            workspace_id=workspace_id, server=server, workspace_path=workspace_path
        )
        understanding = self._from_summary(workspace_id, summary)

        # Compute confidence from the 7 required signals.
        confidence = ConfidenceEngine.compute(
            repository_knowledge=self._signal(len(understanding.entry_points) + len(understanding.services) + len(understanding.db_models)),
            workspace_understanding=0.6 if (understanding.entry_points or understanding.services) else 0.1,
            conversation_history=self._signal(len(conversation_history or [])),
            specification=self._signal(len(specification_text)),
            architecture=self._signal(len(architecture_text)),
            decision_memory=self._signal(len(decision_text)),
            recent_changes=self._signal(len(understanding.changed_files)),
            base=self._last_confidence,
        )
        understanding.confidence = confidence
        self._last_confidence = confidence

        # Escalate only if insufficient and not already a full scan.
        if (confidence.below(Confidence.LOW) or force_inspect) and not understanding.scanned_full:
            logger.info(
                "[workspace] confidence %s insufficient — additional files inspected",
                confidence.value,
            )
            understanding.responsibilities.extend(
                self._index.relevant_to(task=task)[:10]
            )
        return understanding

    def record_verification(self, *, scope: str) -> None:
        """Raise confidence after a successful verification (e.g. tests pass)."""
        self._verified.add(scope)
        item = KnowledgeItem(key=scope, value="verified", confidence=self._last_confidence)
        ConfidenceEngine.increase(item)
        self._last_confidence = item.confidence

    def record_change(self, *, change_type: str) -> None:
        """Lower confidence when repository/architecture changes occur."""
        item = KnowledgeItem(key=change_type, value="changed", confidence=self._last_confidence)
        ConfidenceEngine.decrease(item, reason="repository changes")
        self._last_confidence = item.confidence

    # -- helpers ----------------------------------------------------------- #

    @staticmethod
    def _signal(count_or_ratio: float) -> float:
        """Map a raw count/length to a normalised signal in [0, 1]."""
        v = float(count_or_ratio)
        if v <= 0:
            return 0.0
        if v >= 5:
            return 1.0
        return round(v / 5.0, 3)

    def _from_summary(self, workspace_id: str, summary: dict[str, Any]) -> WorkspaceUnderstanding:
        return WorkspaceUnderstanding(
            workspace_id=workspace_id,
            entry_points=self._index.entry_points(),
            services=self._index.services(),
            db_models=self._index.db_models(),
            responsibilities=self._index.relevant_to(task="")[:10],
            changed_files=summary.get("changed_files") or [],
            scanned_full=bool(summary.get("scanned_full")),
        )

    async def feed_brain(
        self,
        *,
        brain: Any,
        understanding: WorkspaceUnderstanding,
    ) -> bool:
        """Automatically improve the Project Brain from new workspace knowledge.

        Only writes when there is genuinely new information (diff-based, via the
        Context Diff Engine inside ``ProjectBrain``), preserving manual notes.
        Returns True if THINKSYNC.md was updated.
        """
        facts: list[str] = []
        if understanding.services:
            facts.append("Services: " + ", ".join(understanding.services))
        if understanding.entry_points:
            facts.append("Entry points: " + ", ".join(understanding.entry_points))
        if understanding.db_models:
            facts.append("DB models: " + ", ".join(understanding.db_models))
        if understanding.changed_files:
            facts.append("Recent changes: " + ", ".join(understanding.changed_files[:10]))
        if not facts:
            return False
        new_body = "- " + "\n- ".join(facts)
        try:
            await brain.record_change(
                change_type="workspace_awareness",
                description=new_body,
            )
            return True
        except Exception as exc:  # noqa: BLE001 — never block the caller
            logger.warning("[workspace] failed to feed Project Brain: %s", exc)
            return False


__all__ = ["WorkspaceAwareness", "WorkspaceUnderstanding"]
