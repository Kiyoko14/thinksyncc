"""
Context Memory — Sprint 3C.E (Context Engineering).

EXTENSION ONLY. Adds layered, fresh, confident, dependency-aware memory on top
of the existing architecture. Reuses ``services.project_brain`` for durable
storage and ``services.redis_service`` for hot state. Does NOT replace the
existing ``MemoryStore`` (conversation) or ``ContextEngine``.

Components in this module:
    - SessionSnapshot          (session-end snapshot)
    - DecisionMemory           (why-decisions, never rediscovered)
    - ArchitectureMemory       (components/relationships/flow, auto-updated)
    - TaskMemory               (temporary task knowledge, auto-expired)
    - KnowledgeDependencyGraph (impact propagation on change)
    - Freshness                (staleness metadata + invalidation)
    - ConfidenceEngine         (low-confidence -> reload/rebuild)

All persistent write-through goes to ``THINKSYNC.md`` via ``ProjectBrain``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum

from services.project_brain import (
    Confidence,
    EngineeringMemoryLayer,
    KnowledgeItem,
    ProjectBrain,
    _conf_to_float,
    _float_to_conf,
)

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Freshness
# --------------------------------------------------------------------------- #

_STALE_DAYS = {"permanent": 365, "architecture": 90, "repository": 30, "sprint": 14, "task": 3, "conversation": 1}


class Freshness:
    """Track and invalidate stale knowledge by layer."""

    @staticmethod
    def is_stale(item: KnowledgeItem, *, now: datetime | None = None) -> bool:
        try:
            ts = datetime.fromisoformat(item.updated)
        except ValueError:
            return True
        age_days = (now or datetime.now(timezone.utc) - ts).days
        limit = _STALE_DAYS.get(item.layer.value, 30)
        return age_days > limit

    @staticmethod
    def refresh_metadata(item: KnowledgeItem, *, origin: str = "agent") -> KnowledgeItem:
        item.updated = datetime.now(timezone.utc).isoformat()
        item.version += 1
        item.origin = origin
        return item


# --------------------------------------------------------------------------- #
# Confidence Engine
# --------------------------------------------------------------------------- #

class ConfidenceEngine:
    """Decide whether stored knowledge is trustworthy enough to reuse.

    Production-grade confidence (Sprint 3F1, PART 3): confidence is *computed*
    from multiple signals rather than a single stale flag. After successful
    verification it increases; after repository/architecture changes,
    contradictions, failed assumptions or outdated knowledge it decreases.
    When confidence drops to/below the reload threshold OR knowledge is stale,
    callers should reload ONLY the required knowledge, never rebuild everything.
    """

    RELOAD_THRESHOLD = Confidence.LOW

    @staticmethod
    def should_reload(item: KnowledgeItem) -> bool:
        if Freshness.is_stale(item):
            return True
        return item.confidence.below(ConfidenceEngine.RELOAD_THRESHOLD)

    @staticmethod
    def downgrade(item: KnowledgeItem) -> KnowledgeItem:
        order = [Confidence.HIGH, Confidence.MEDIUM, Confidence.LOW]
        idx = order.index(item.confidence)
        if idx < len(order) - 1:
            item.confidence = order[idx + 1]
        return item

    # -- computed confidence (7 signals) ---------------------------------- #

    @staticmethod
    def compute(
        *,
        repository_knowledge: float = 0.0,
        workspace_understanding: float = 0.0,
        conversation_history: float = 0.0,
        specification: float = 0.0,
        architecture: float = 0.0,
        decision_memory: float = 0.0,
        recent_changes: float = 0.0,
        base: Confidence = Confidence.MEDIUM,
    ) -> Confidence:
        """Compute a confidence level from 7 normalised signals in [0, 1].

        Each signal reflects how well we understand that dimension. The result
        is a weighted blend; a strong signal in any single dimension cannot
        fully rescue an otherwise-blind agent (min-floor applied), and a total
        blackout forces LOW.
        """
        signals = {
            repository_knowledge,
            workspace_understanding,
            conversation_history,
            specification,
            architecture,
            decision_memory,
            recent_changes,
        }
        # All signals must be valid floats in [0, 1].
        clean = [max(0.0, min(1.0, float(s))) for s in signals]
        if not clean:
            return Confidence.LOW
        mean = sum(clean) / len(clean)
        # Floor: if the weakest signal is very poor, confidence is at most MEDIUM.
        weakest = min(clean)
        floor = Confidence.LOW if weakest < 0.2 else (Confidence.MEDIUM if weakest < 0.5 else Confidence.HIGH)
        # Weighted blend toward the floor so a single good signal cannot mask
        # a blind spot.
        blended = mean * 0.7 + _conf_to_float(floor) * 0.3
        return _float_to_conf(blended)

    @staticmethod
    def increase(item: KnowledgeItem, *, amount: float = 0.2) -> KnowledgeItem:
        """Raise confidence after a successful verification (e.g. tests pass)."""
        cur = _conf_to_float(item.confidence)
        new = _float_to_conf(min(1.0, cur + amount))
        item.confidence = new
        return item

    @staticmethod
    def decrease(
        item: KnowledgeItem,
        *,
        reason: str = "knowledge changed",
    ) -> KnowledgeItem:
        """Lower confidence after a change/contradiction/failed assumption.

        Reasons recognised (each triggers the same safe downgrade, but the
        reason is recorded on the item so callers can decide reload scope):
          - repository changes
          - architecture changes
          - contradicting information
          - failed assumptions
          - outdated knowledge
        """
        item.confidence = ConfidenceEngine.downgrade(item).confidence
        item.origin = f"decreased:{reason}"
        return item


# --------------------------------------------------------------------------- #
# Session Snapshot
# --------------------------------------------------------------------------- #

@dataclass
class SessionSnapshotData:
    """Snapshot emitted at the end of a work session."""

    goal: str = ""
    completed: list[str] = field(default_factory=list)
    progress: str = ""
    blockers: list[str] = field(default_factory=list)
    pending_tasks: list[str] = field(default_factory=list)
    open_questions: list[str] = field(default_factory=list)
    next_step: str = ""
    timestamp: str = ""

    def to_markdown(self) -> str:
        def bullet(items: list[str]) -> str:
            return "\n".join(f"- {i}" for i in items) or "- (none)"

        return (
            f"- **Goal:** {self.goal}\n"
            f"- **Completed:**\n{bullet(self.completed)}\n"
            f"- **Progress:** {self.progress}\n"
            f"- **Blockers:**\n{bullet(self.blockers)}\n"
            f"- **Pending:**\n{bullet(self.pending_tasks)}\n"
            f"- **Open questions:**\n{bullet(self.open_questions)}\n"
            f"- **Next step:** {self.next_step}\n"
            f"- _updated: {self.timestamp or datetime.now(timezone.utc).isoformat()}_"
        )

    @classmethod
    def from_markdown(cls, text: str) -> "SessionSnapshotData":
        data = cls()
        key = None
        for line in (text or "").splitlines():
            line = line.strip()
            if line.startswith("- **Goal:**"):
                data.goal = _strip_bold(line.split(":", 1)[1].strip())
            elif line.startswith("- **Completed:**"):
                key = "completed"
            elif line.startswith("- **Progress:**"):
                data.progress = _strip_bold(line.split(":", 1)[1].strip())
                key = None
            elif line.startswith("- **Blockers:**"):
                key = "blockers"
            elif line.startswith("- **Pending:**"):
                key = "pending_tasks"
            elif line.startswith("- **Open questions:**"):
                key = "open_questions"
            elif line.startswith("- **Next step:**"):
                data.next_step = _strip_bold(line.split(":", 1)[1].strip())
                key = None
            elif line.startswith("- _updated:"):
                data.timestamp = line.split(":", 1)[1].strip().rstrip("_").strip()
            elif line.startswith("- ") and key:
                getattr(data, key).append(_strip_bold(line[2:].strip()))
        return data


class SessionSnapshot:
    """Persist/load the session snapshot via ProjectBrain (THINKSYNC.md)."""

    def __init__(self, brain: ProjectBrain) -> None:
        self._brain = brain

    async def save(self, *, snapshot: SessionSnapshotData) -> None:
        snapshot.timestamp = datetime.now(timezone.utc).isoformat()
        await self._brain.save_session_snapshot(snapshot=snapshot)

    async def load(self) -> SessionSnapshotData | None:
        body = await self._brain.get_section("Session Snapshot")
        if not body:
            return None
        return SessionSnapshotData.from_markdown(body)


# --------------------------------------------------------------------------- #
# Decision Memory
# --------------------------------------------------------------------------- #

@dataclass
class DecisionRecord:
    title: str
    rationale: str
    confidence: Confidence = Confidence.MEDIUM
    updated: str = ""

    def to_item(self) -> KnowledgeItem:
        return KnowledgeItem(
            key=f"decision:{_slug(self.title)}",
            value=f"{self.title} :: {self.rationale}",
            layer=EngineeringMemoryLayer.PERMANENT,
            confidence=self.confidence,
            updated=self.updated or datetime.now(timezone.utc).isoformat(),
        )


class DecisionMemory:
    """Persist engineering decisions so future sessions reuse them."""

    def __init__(self, brain: ProjectBrain) -> None:
        self._brain = brain

    async def record(self, *, title: str, rationale: str, confidence: Confidence = Confidence.MEDIUM) -> None:
        await self._brain.append_decision(title=title, rationale=rationale, confidence=confidence)


# --------------------------------------------------------------------------- #
# Architecture Memory
# --------------------------------------------------------------------------- #

@dataclass
class ArchitectureNode:
    name: str
    kind: str  # component | service | workflow | dataflow
    owner: str = ""
    description: str = ""
    confidence: Confidence = Confidence.MEDIUM


class ArchitectureMemory:
    """Maintain architecture understanding; updates when architecture changes."""

    def __init__(self, brain: ProjectBrain) -> None:
        self._brain = brain
        self._nodes: dict[str, ArchitectureNode] = {}

    def register(self, *, node: ArchitectureNode) -> None:
        self._nodes[node.name] = node

    async def sync_to_brain(self) -> None:
        """Write the architecture overview into THINKSYNC.md (diff-based)."""
        lines = ["Components and their ownership:"]
        for node in sorted(self._nodes.values(), key=lambda n: n.name):
            owner = f" (owner: {node.owner})" if node.owner else ""
            lines.append(f"- **{node.name}** [{node.kind}]{owner} — {node.description}")
        body = "\n".join(lines)
        # Use the high-level architecture section as the durable home.
        await self._brain.set_section("Architecture (high level)", body)


# --------------------------------------------------------------------------- #
# Task Memory
# --------------------------------------------------------------------------- #

@dataclass
class TaskRecord:
    objective: str
    files: list[str] = field(default_factory=list)
    progress: str = ""
    blocked: bool = False
    remaining: list[str] = field(default_factory=list)
    updated: str = ""

    def to_item(self) -> KnowledgeItem:
        return KnowledgeItem(
            key=f"task:{_slug(self.objective)[:40]}",
            value=f"obj={self.objective} | files={','.join(self.files)} | "
                  f"progress={self.progress} | blocked={self.blocked} | "
                  f"remaining={','.join(self.remaining)}",
            layer=EngineeringMemoryLayer.TASK,
            confidence=Confidence.MEDIUM,
            updated=self.updated or datetime.now(timezone.utc).isoformat(),
        )


class TaskMemory:
    """Temporary task knowledge; auto-removed when no longer useful."""

    def __init__(self, brain: ProjectBrain) -> None:
        self._brain = brain
        self._active: dict[str, TaskRecord] = {}

    def begin(self, *, record: TaskRecord) -> None:
        self._active[_slug(record.objective)[:40]] = record

    def complete(self, *, objective: str) -> None:
        key = _slug(objective)[:40]
        self._active.pop(key, None)

    def current(self) -> TaskRecord | None:
        # Most recently begun still-active task.
        if not self._active:
            return None
        return list(self._active.values())[-1]


# --------------------------------------------------------------------------- #
# Knowledge Dependency Graph
# --------------------------------------------------------------------------- #

class DependencyKind(str, Enum):
    DEPENDS_ON = "depends_on"
    OWNS = "owns"
    TRIGGERS = "triggers"


@dataclass
class GraphEdge:
    src: str
    dst: str
    kind: DependencyKind = DependencyKind.DEPENDS_ON


class KnowledgeDependencyGraph:
    """Maintain relationships between knowledge domains.

    Example chain: Workspace -> Server -> Deployment -> Approval -> Resume ->
    Implementation -> Context. If one node changes, only affected downstream
    knowledge is refreshed (never the whole Project Brain).
    """

    def __init__(self) -> None:
        self._edges: list[GraphEdge] = []
        self._nodes: set[str] = set()

    def add_edge(self, src: str, dst: str, kind: DependencyKind = DependencyKind.DEPENDS_ON) -> None:
        self._nodes.add(src)
        self._nodes.add(dst)
        if not any(e.src == src and e.dst == dst for e in self._edges):
            self._edges.append(GraphEdge(src, dst, kind))

    def downstream(self, node: str) -> list[str]:
        """Return all nodes transitively affected by a change to ``node``."""
        result: list[str] = []
        stack = [node]
        seen = set()
        while stack:
            cur = stack.pop()
            for e in self._edges:
                if e.src == cur and e.dst not in seen:
                    seen.add(e.dst)
                    result.append(e.dst)
                    stack.append(e.dst)
        return result

    def build_default(self) -> None:
        """Wire the canonical ThinkSync knowledge chain."""
        chain = [
            "workspace", "server", "deployment", "approval",
            "resume", "implementation", "context",
        ]
        for a, b in zip(chain, chain[1:]):
            self.add_edge(a, b, DependencyKind.DEPENDS_ON)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _slug(text: str) -> str:
    import re as _re
    return _re.sub(r"[^a-z0-9]+", "_", (text or "").lower()).strip("_") or "unknown"


def _strip_bold(text: str) -> str:
    return text.strip().lstrip("*").rstrip("*").strip()


__all__ = [
    "Freshness",
    "ConfidenceEngine",
    "SessionSnapshotData",
    "SessionSnapshot",
    "DecisionRecord",
    "DecisionMemory",
    "ArchitectureNode",
    "ArchitectureMemory",
    "TaskRecord",
    "TaskMemory",
    "DependencyKind",
    "GraphEdge",
    "KnowledgeDependencyGraph",
]
