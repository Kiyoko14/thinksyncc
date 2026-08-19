"""
Project Brain — Sprint 3C.E (Context Engineering).

EXTENSION ONLY. Does NOT replace or modify the existing ContextEngine
(`services/context_engine.py`). This module adds a persistent *long-term*
engineering memory layer on top of the existing architecture.

The Project Brain's durable store is ``THINKSYNC.md`` (an AI-only file, NOT a
README). It evolves incrementally: sections are patched in place, the file is
never regenerated wholesale (Context Diff Engine). Obsolete temporary knowledge
is archived by the Memory Garbage Collector.

Reused infrastructure (no parallel implementations):
    - ``services.redis_service.RedisService``  (hot cache)
    - ``core.config.get_settings``
    - ``core.database.get_supabase``            (optional persistence)

All new behaviour is surfaced through the orchestration pipeline via
``services.progressive_context``; this module is pure storage/intelligence.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any, TYPE_CHECKING

from services.redis_service import RedisService

if TYPE_CHECKING:
    from services.context_memory import SessionSnapshotData

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Module-level helpers (must precede classes that use them at definition time)
# --------------------------------------------------------------------------- #

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()


def _looks_like_task_entry(line: str) -> bool:
    low = line.lower()
    return any(tok in low for tok in ("[done]", "[completed]", "[expired]", "[archived]"))


# --------------------------------------------------------------------------- #
# Confidence + memory layers
# --------------------------------------------------------------------------- #


class Confidence(str, Enum):
    """Confidence level attached to every knowledge item."""

    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"

    @property
    def rank(self) -> int:
        return {"high": 3, "medium": 2, "low": 1}[self.value]

    def below(self, other: "Confidence") -> bool:
        # inclusive: same-or-lower rank triggers reload
        return self.rank <= other.rank


def _conf_to_float(c: Confidence) -> float:
    """Map a ``Confidence`` to a numeric score in [0, 1]."""
    return {"high": 1.0, "medium": 0.6, "low": 0.25}[c.value]


def _float_to_conf(score: float) -> Confidence:
    """Map a numeric score in [0, 1] to a ``Confidence`` level."""
    s = max(0.0, min(1.0, float(score)))
    if s >= 0.8:
        return Confidence.HIGH
    if s >= 0.45:
        return Confidence.MEDIUM
    return Confidence.LOW


class EngineeringMemoryLayer(str, Enum):
    """Distinct memory lifetimes. Permanent knowledge must never be mixed with
    temporary (task/conversation) knowledge."""

    PERMANENT = "permanent"
    ARCHITECTURE = "architecture"
    PROJECT_BRAIN = "project_brain"
    REPOSITORY = "repository"
    SPRINT = "sprint"
    TASK = "task"
    CONVERSATION = "conversation"

    # Permanent layers must never be garbage-collected.
    PERMANENT_LAYERS = {PERMANENT, ARCHITECTURE, PROJECT_BRAIN, REPOSITORY}


# --------------------------------------------------------------------------- #
# Knowledge item (carries freshness + confidence metadata)
# --------------------------------------------------------------------------- #


@dataclass
class KnowledgeItem:
    """A single piece of engineering knowledge with provenance metadata."""

    key: str
    value: str
    layer: EngineeringMemoryLayer = EngineeringMemoryLayer.PROJECT_BRAIN
    origin: str = "agent"
    confidence: Confidence = Confidence.MEDIUM
    version: int = 1
    updated: str = ""
    superseded_by: str | None = None

    def __post_init__(self) -> None:
        if not self.updated:
            self.updated = _now_iso()

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "value": self.value,
            "layer": self.layer.value,
            "origin": self.origin,
            "confidence": self.confidence.value,
            "version": self.version,
            "updated": self.updated,
            "superseded_by": self.superseded_by,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "KnowledgeItem":
        return cls(
            key=str(data.get("key", "")),
            value=str(data.get("value", "")),
            layer=EngineeringMemoryLayer(data.get("layer", "project_brain")),
            origin=str(data.get("origin", "agent")),
            confidence=Confidence(data.get("confidence", "medium")),
            version=int(data.get("version", 1)),
            updated=str(data.get("updated", "")),
            superseded_by=data.get("superseded_by"),
        )


# --------------------------------------------------------------------------- #
# Context Diff Engine — minimal-change updates to THINKSYNC.md
# --------------------------------------------------------------------------- #


class ContextDiffEngine:
    """Produce the smallest possible edit to THINKSYNC.md.

    Never rewrites the whole file. Locates a named section (``## N. Title``)
    and replaces only its body. Manual notes outside recognised sections are
    always preserved because we only splice the targeted block.
    """

    _SECTION_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*$", re.MULTILINE)

    @staticmethod
    def find_section(text: str, title: str) -> tuple[int, int, str] | None:
        """Return (start, end, matched_title) for the first section whose
        title (case-insensitive, normalised) matches ``title``."""
        target = _norm(title)
        lines = text.splitlines()
        for i, line in enumerate(lines):
            m = ContextDiffEngine._SECTION_RE.match(line)
            if m and _norm(m.group(2)) == target:
                # End = next heading of same-or-higher level, or EOF.
                level = len(m.group(1))
                end = len(lines)
                for j in range(i + 1, len(lines)):
                    nm = ContextDiffEngine._SECTION_RE.match(lines[j])
                    if nm and len(nm.group(1)) <= level:
                        end = j
                        break
                return i, end, m.group(2)
        return None

    @staticmethod
    def patch_section(text: str, title: str, body: str) -> tuple[str, bool]:
        """Replace (or append) the body of ``title``. Returns (new_text, changed)."""
        located = ContextDiffEngine.find_section(text, title)
        body_clean = body.rstrip() + "\n"
        if located is None:
            # Append as a new section.
            trimmed = text.rstrip()
            separator = "\n\n" if trimmed else ""
            new_text = f"{trimmed}{separator}\n## {title}\n\n{body_clean}"
            return new_text + "\n", True
        start, end, matched = located
        lines = text.splitlines()
        # Old body is everything between the header line and the section end.
        old_body = "\n".join(lines[start + 1:end]).strip()
        if old_body == body_clean.rstrip():
            # Already identical — no change needed.
            return text, False
        new_lines = lines[:start] + [f"## {matched}", "", body_clean.rstrip()] + lines[end:]
        new_text = "\n".join(new_lines).rstrip() + "\n"
        return new_text, True

    @staticmethod
    def diff_items(
        previous: dict[str, KnowledgeItem],
        current: dict[str, KnowledgeItem],
    ) -> dict[str, Any]:
        """Return only the minimal change-set between two item maps."""
        added = [k for k in current if k not in previous]
        removed = [k for k in previous if k not in current]
        changed = [
            k
            for k in current
            if k in previous and previous[k].value != current[k].value
        ]
        return {
            "added": added,
            "removed": removed,
            "changed": changed,
            "unchanged": [
                k for k in current if k in previous and previous[k].value == current[k].value
            ],
        }


# --------------------------------------------------------------------------- #
# Memory Garbage Collector
# --------------------------------------------------------------------------- #


class MemoryGarbageCollector:
    """Detect and safely archive/remove *temporary* knowledge only.

    Permanent layers (architecture, project brain, repository, security,
    design rationale, production knowledge) are NEVER removed.
    """

    # Section markers whose content is permanent and must be preserved.
    # Stored pre-normalised (run through _norm semantics: non-alnum -> space).
    PERMANENT_SECTIONS = {
        _norm(s) for s in {
            "product & mission",
            "technology stack",
            "architecture (high level)",
            "coding conventions",
            "key design decisions (per decision memory)",
            "security decisions",
            "production constraints",
        }
    }

    @staticmethod
    def is_permanent(section_title: str) -> bool:
        return _norm(section_title) in MemoryGarbageCollector.PERMANENT_SECTIONS

    @staticmethod
    def should_archive(item: KnowledgeItem, *, now: datetime | None = None) -> bool:
        """True when a temporary item is eligible for archival."""
        if item.layer in EngineeringMemoryLayer.PERMANENT_LAYERS:
            return False
        if item.superseded_by:
            return True
        # Stale low-confidence temporary knowledge.
        if item.confidence == Confidence.LOW and item.layer in {
            EngineeringMemoryLayer.TASK,
            EngineeringMemoryLayer.SPRINT,
            EngineeringMemoryLayer.CONVERSATION,
        }:
            try:
                ts = datetime.fromisoformat(item.updated)
                age_days = (now or datetime.now(timezone.utc) - ts).days
                if age_days > 7:
                    return True
            except ValueError:
                return False
        return False


# --------------------------------------------------------------------------- #
# Project Brain — the persistent long-term engineering memory
# --------------------------------------------------------------------------- #


@dataclass
class _BrainConfig:
    path: str = "/root/thinksync/THINKSYNC.md"
    cache_ttl: int = 60 * 60 * 24  # 24h hot cache


class ProjectBrain:
    """Persistent Project Brain stored in THINKSYNC.md.

    The brain is the single source of truth for long-term engineering
    knowledge. It is updated incrementally (Context Diff Engine) and cleaned
    by the Memory Garbage Collector. Hot reads go through Redis.
    """

    _CACHE_PREFIX = "project_brain"

    def __init__(self, config: _BrainConfig | None = None) -> None:
        self._config = config or _BrainConfig()
        self._items: dict[str, KnowledgeItem] = {}

    # -- file IO (local disk; fast enough for a single repo file) ---------- #

    def _read_file(self) -> str:
        try:
            with open(self._config.path, "r", encoding="utf-8") as fh:
                return fh.read()
        except FileNotFoundError:
            return _DEFAULT_THINKSYNC.strip() + "\n"
        except OSError as exc:
            logger.warning("ProjectBrain: cannot read %s: %s", self._config.path, exc)
            return _DEFAULT_THINKSYNC.strip() + "\n"

    def _write_file(self, text: str) -> None:
        try:
            os.makedirs(os.path.dirname(self._config.path), exist_ok=True)
            with open(self._config.path, "w", encoding="utf-8") as fh:
                fh.write(text)
        except OSError as exc:
            logger.warning("ProjectBrain: cannot write %s: %s", self._config.path, exc)

    # -- hot cache ----------------------------------------------------------- #

    async def _cache_get(self, key: str) -> str | None:
        redis = RedisService.get_async_client()
        if redis is None:
            return None
        try:
            return await redis.get(f"{self._CACHE_PREFIX}:{key}")
        except Exception as exc:  # noqa: BLE001 — best-effort cache only
            logger.debug("ProjectBrain cache get failed: %s", exc)
            return None

    async def _cache_set(self, key: str, value: str) -> None:
        redis = RedisService.get_async_client()
        if redis is None:
            return
        try:
            await redis.set(
                f"{self._CACHE_PREFIX}:{key}", value, ex=self._config.cache_ttl
            )
        except Exception as exc:  # noqa: BLE001 — best-effort cache only
            logger.debug("ProjectBrain cache set failed: %s", exc)

    # -- public: section access (diff-based) -------------------------------- #

    async def get_section(self, title: str) -> str | None:
        cached = await self._cache_get(f"section:{_norm(title)}")
        if cached is not None:
            return cached or None
        text = self._read_file()
        located = ContextDiffEngine.find_section(text, title)
        if located is None:
            await self._cache_set(f"section:{_norm(title)}", "")
            return None
        start, end, _ = located
        lines = text.splitlines()
        body = "\n".join(lines[start + 1:end]).strip()
        await self._cache_set(f"section:{_norm(title)}", body)
        return body or None

    async def set_section(self, title: str, body: str) -> bool:
        """Update a single section minimally. Returns True if file changed."""
        text = self._read_file()
        new_text, changed = ContextDiffEngine.patch_section(text, title, body)
        if changed:
            self._write_file(new_text)
            await self._cache_set(f"section:{_norm(title)}", body.strip())
        return changed

    # -- public: decisions (Decision Memory backing) ----------------------- #

    async def append_decision(self, *, title: str, rationale: str, confidence: Confidence = Confidence.MEDIUM) -> None:
        existing = await self.get_section("Key Design Decisions (per Decision Memory)") or ""
        entry = f"- **{title}** — {rationale}  _(confidence: {confidence.value})_"
        # Avoid duplicate decisions.
        if _norm(title) in _norm(existing):
            # Update in place rather than duplicate.
            new_lines = []
            replaced = False
            for line in existing.splitlines():
                if _norm(title) in _norm(line) and not replaced:
                    new_lines.append(entry)
                    replaced = True
                else:
                    new_lines.append(line)
            await self.set_section("Key Design Decisions (per Decision Memory)", "\n".join(new_lines))
            return
        updated = (existing + "\n" + entry).strip()
        await self.set_section("Key Design Decisions (per Decision Memory)", updated)

    # -- public: limitations (GC-managed) ----------------------------------- #

    async def append_limitation(self, *, text: str) -> None:
        existing = await self.get_section("Known Limitations / Technical Debt") or ""
        if _norm(text) in _norm(existing):
            return
        updated = (existing + "\n- " + text).strip()
        await self.set_section("Known Limitations / Technical Debt", updated)

    # -- public: session snapshot ------------------------------------------- #

    async def save_session_snapshot(self, *, snapshot: "SessionSnapshotData") -> None:
        body = snapshot.to_markdown()
        await self.set_section("Session Snapshot", body)

    # -- public: self-learning hook ----------------------------------------- #

    async def record_change(
        self,
        *,
        change_type: str,
        description: str,
        confidence: Confidence = Confidence.MEDIUM,
    ) -> None:
        """Called after meaningful engineering changes (self-learning)."""
        await self.append_limitation(text=f"{change_type}: {description}")
        # Self-learning also refreshes the current-sprint note.
        sprint = await self.get_section("Current Sprint & Roadmap") or ""
        if "Sprint 3C.E" not in sprint:
            await self.set_section(
                "Current Sprint & Roadmap",
                sprint + "\n- **Current:** Sprint 3C.E — Context Engineering.",
            )

    # -- public: garbage collection ---------------------------------------- #

    async def garbage_collect(self) -> dict[str, int]:
        """Archive/remove only temporary, expired, superseded knowledge."""
        text = self._read_file()
        lines = text.splitlines()
        new_lines: list[str] = []
        removed = 0
        in_temp_section = False
        for line in lines:
            m = ContextDiffEngine._SECTION_RE.match(line)
            if m:
                in_temp_section = not MemoryGarbageCollector.is_permanent(m.group(2))
                new_lines.append(line)
                continue
            if in_temp_section and _looks_like_task_entry(line):
                # Heuristic: completed/expired temporary task lines dropped.
                removed += 1
                continue
            new_lines.append(line)
        if removed:
            self._write_file("\n".join(new_lines).rstrip() + "\n")
        return {"removed_temp_entries": removed}


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


_DEFAULT_THINKSYNC = """
# THINKSYNC.md — Engineering Memory (AI-only)

> This file is NOT a README. It is the persistent Project Brain for ThinkSync.
> Updated incrementally by the Context Engineering layer.

## Product & Mission

ThinkSync turns natural-language objectives into running applications.

## Technology Stack

- Backend: Python (FastAPI-style), Supabase (Postgres + RLS).
- LLM: OpenAI-compatible endpoint (configurable).
- Cache: Redis (optional).

## Architecture (high level)

user objective -> Approval/Clarification -> Planner -> Implementation
Intelligence -> Context Engine -> Executor -> Resume/Wait -> Deployment

## Coding Conventions

- `from __future__ import annotations`.
- Typed exceptions over bare `except Exception`.

## Key Design Decisions (per Decision Memory)

## Current Sprint & Roadmap

## Known Limitations / Technical Debt

## Security Decisions

## Production Constraints

## Session Snapshot
"""


# Re-export so downstream modules import from one place.
__all__ = [
    "Confidence",
    "EngineeringMemoryLayer",
    "KnowledgeItem",
    "ContextDiffEngine",
    "MemoryGarbageCollector",
    "ProjectBrain",
]
