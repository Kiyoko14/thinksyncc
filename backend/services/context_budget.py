"""
Context Budget Manager + Automatic Context Compression — Sprint 3C.E.

EXTENSION ONLY. Governs token usage across context layers and compresses
completed work while preserving engineering-critical knowledge. Reuses the
existing config limits (``AGENT_CONTEXT_*``) and never duplicates the snippet
selection logic in ``services.context_engine``.

Priority order (highest first):
    current_task > project_brain > architecture > specification >
    repository_index > relevant_files > conversation

Low-value context is discarded first; the configured budget is never exceeded.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Any

from core.config import get_settings

logger = logging.getLogger(__name__)


class ContextPriority(str, Enum):
    """Higher number = kept first when budget is tight."""

    CURRENT_TASK = "current_task"
    PROJECT_BRAIN = "project_brain"
    ARCHITECTURE = "architecture"
    SPECIFICATION = "specification"
    REPOSITORY_INDEX = "repository_index"
    RELEVANT_FILES = "relevant_files"
    CONVERSATION = "conversation"

    @property
    def weight(self) -> int:
        return {
            "current_task": 100,
            "project_brain": 90,
            "architecture": 80,
            "specification": 70,
            "repository_index": 60,
            "relevant_files": 50,
            "conversation": 40,
        }[self.value]


# Rough token estimator: ~4 chars per token (conservative for code/English mix).
_CHARS_PER_TOKEN = 4


def estimate_tokens(text: str) -> int:
    if not text:
        return 0
    return max(1, len(text) // _CHARS_PER_TOKEN)


@dataclass
class _BudgetConfig:
    max_tokens: int = 0  # resolved from settings at runtime
    overflow_keep_top: int = 12  # conversation turns kept when compressing


class ContextBudgetManager:
    """Allocate and enforce a token budget across prioritized context blocks."""

    def __init__(self, config: _BudgetConfig | None = None) -> None:
        self._config = config or _BudgetConfig()
        settings = get_settings()
        if self._config.max_tokens <= 0:
            # Derive a sensible budget from the existing per-file limits.
            self._config.max_tokens = max(
                400,
                int(settings.AGENT_CONTEXT_MAX_TOTAL_LINES) * 8
                + int(settings.AGENT_CONTEXT_MAX_FILES) * 120,
            )

    @property
    def budget(self) -> int:
        return self._config.max_tokens

    def prioritise(self, blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Order blocks by priority weight (desc). Each block must carry
        ``priority`` (ContextPriority) and ``content`` (str)."""
        for b in blocks:
            prio = b.get("priority")
            b["_weight"] = prio.weight if isinstance(prio, ContextPriority) else 0
        return sorted(blocks, key=lambda b: -b["_weight"])

    def fit(self, blocks: list[dict[str, Any]]) -> dict[str, Any]:
        """Return {included, dropped, total_tokens, budget}.

        Higher-priority blocks are kept; lower-priority are dropped first when
        the budget is exceeded. Returns which blocks were included/dropped.
        """
        ordered = self.prioritise(blocks)
        used = 0
        included: list[str] = []
        dropped: list[str] = []
        for b in ordered:
            name = str(b.get("name", "block"))
            cost = estimate_tokens(b.get("content", ""))
            if used + cost > self.budget:
                dropped.append(name)
                continue
            included.append(name)
            used += cost
        return {
            "included": included,
            "dropped": dropped,
            "total_tokens": used,
            "budget": self.budget,
        }


# --------------------------------------------------------------------------- #
# Automatic Context Compression
# --------------------------------------------------------------------------- #

# Knowledge classes that must survive compression (never discarded).
_PRESERVE_KEYWORDS = (
    "decision", "architecture", "rationale", "security", "production",
    "contract", "api", "database", "schema", "design", "specification",
    "pending", "question", "open", "repository",
)


class ContextCompressor:
    """Compress completed work while preserving engineering-critical info."""

    def __init__(self, keep_recent: int = 12) -> None:
        self._keep_recent = keep_recent

    def compress_conversation(self, turns: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Keep only the most recent ``keep_recent`` turns plus any turn that
        carries a preserved engineering fact."""
        if not turns:
            return []
        preserved: list[dict[str, Any]] = []
        for t in turns:
            text = str(t.get("content", "")).lower()
            if any(k in text for k in _PRESERVE_KEYWORDS):
                preserved.append(t)
        recent = turns[-self._keep_recent:]
        # Merge without duplicating.
        seen = {id(t) for t in recent}
        result = list(recent)
        for t in preserved:
            if id(t) not in seen:
                result.append(t)
        return result

    def compress_summary(self, text: str) -> str:
        """Collapse a context block to its engineering-essential sentences."""
        if not text:
            return ""
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        kept = [
            ln for ln in lines
            if any(k in ln.lower() for k in _PRESERVE_KEYWORDS)
        ]
        if not kept:
            # No engineering keywords: keep first + last line as a minimal stub.
            return (lines[0] + (" … " + lines[-1] if len(lines) > 1 else "")).strip()
        return "\n".join(kept)


__all__ = [
    "ContextPriority",
    "ContextBudgetManager",
    "ContextCompressor",
    "estimate_tokens",
]
