"""ExecutionResult — single source of truth for completed execution.

The agent orchestrator (`run_agent_pipeline`) previously derived the final
user-facing response from `result.get("summary") or result.get("message")`,
which are almost always empty, collapsing every successful job into the
generic "All set." fallback.

`ExecutionResult` replaces that scattered, lossy pattern. It is built ONCE at
completion from the internal execution result dict, the resolved workspace row,
the implementation strategy, and the template decision. Every downstream
consumer (final HTTP response, conversation history, ContextEngine) reads this
single structured object — never the raw progress stream.

Design rules
------------
* Read-only projection target. It owns business result data; it does NOT execute
  tools and it does NOT reconstruct information from live progress events.
* Separates workspace identity: `workspace_id` (DB key), `workspace_slug`
  (internal), `workspace_display_name` (human-readable, user-facing only).
* Deterministic and minimal. No LLM call is required to build it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class ExecutionResult:
    # --- Workspace identity (separated) -------------------------------------
    workspace_id: str = ""
    workspace_display_name: str = ""   # user's "My Telegram Bot" — never slug
    workspace_slug: str = ""           # internal only
    workspace_path: str = ""           # internal only

    # --- Strategy / template ------------------------------------------------
    strategy: str = ""                 # EXACT_TEMPLATE | HYBRID_TEMPLATE_AI | PURE_AI_GENERATION
    template_name: str | None = None   # e.g. "telegram/basic_bot"
    template_summary: str | None = None

    # --- Files --------------------------------------------------------------
    created_files: list[str] = field(default_factory=list)
    modified_files: list[str] = field(default_factory=list)
    deleted_files: list[str] = field(default_factory=list)

    # --- Execution evidence -------------------------------------------------
    success: bool = True
    status: str = "completed"          # completed | failed
    tests: dict[str, Any] | None = None      # {passed, failed, summary}
    warnings: list[str] = field(default_factory=list)
    errors: list[str] | None = None
    patches: list[str] | None = None
    deployment: dict[str, Any] | None = None  # {url, verified}
    artifacts: dict[str, Any] | None = None
    logs_tail: str | None = None       # truncated, internal-only

    # --- Narrative (projected, never raw) -----------------------------------
    summary: str = ""                  # user-facing markdown summary

    def to_dict(self) -> dict[str, Any]:
        """Serialize for persistence / API responses (no internal secrets)."""
        return {
            "workspace_id": self.workspace_id,
            "workspace_display_name": self.workspace_display_name,
            "workspace_slug": self.workspace_slug,
            "workspace_path": self.workspace_path,
            "strategy": self.strategy,
            "template_name": self.template_name,
            "template_summary": self.template_summary,
            "created_files": self.created_files,
            "modified_files": self.modified_files,
            "deleted_files": self.deleted_files,
            "success": self.success,
            "status": self.status,
            "tests": self.tests,
            "warnings": self.warnings,
            "errors": self.errors,
            "patches": self.patches,
            "deployment": self.deployment,
            "artifacts": self.artifacts,
            "summary": self.summary,
        }
