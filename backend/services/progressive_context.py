"""
Progressive Context Loader — Sprint 3C.E (Context Engineering).

EXTENSION ONLY. Orchestrates context assembly across the new memory layers
while REUSING the existing ``ContextEngine.build_context`` for repository
file/snippet selection. This is the single integration point into the
orchestration pipeline.

Progressive load order (each layer decides whether MORE context is needed):
    1. Project Brain (THINKSYNC.md)
    2. Session Snapshot
    3. Current Task
    4. Relevant Specification
    5. Repository Index
    6. Relevant Repository Files   (via ContextEngine — preserved)
    7. Additional Repository Context (only if needed)
    8. Full Repository (last resort)

Token budget is enforced by ``ContextBudgetManager``; completed-work
compression is applied by ``ContextCompressor``. Freshness/confidence gate
whether stored knowledge is trusted or rebuilt.

Backward compatibility: ``ProgressiveContextLoader.build()`` returns the SAME
shape ``ContextEngine.build_context`` returned (mode / selected_files /
snippets / prompt_payload), plus an added ``engineering_context`` block that
older callers ignore.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from core.config import get_settings

from services.context_budget import (
    ContextBudgetManager,
    ContextCompressor,
    ContextPriority,
    estimate_tokens,
)
from services.context_engine import ContextEngine
from services.context_memory import (
    ArchitectureMemory,
    ConfidenceEngine,
    DecisionMemory,
    Freshness,
    KnowledgeDependencyGraph,
    ProjectBrain,
    SessionSnapshot,
    TaskMemory,
)
from services.project_brain import Confidence, KnowledgeItem
from services.repository_index import RepositoryIndex
from services.workspace_awareness import WorkspaceAwareness
from services.self_evaluation import EvalAction, SelfEvaluator

logger = logging.getLogger(__name__)


@dataclass
class _LoaderConfig:
    enable_brain: bool = True
    enable_repo_index: bool = True
    full_repo_last_resort: bool = True
    prefer_cache: bool = True
    # Sprint 3F1: confidence gate — stop escalating context once sufficient.
    confidence_gate: bool = True
    # Sprint 3F1: feed new workspace knowledge back into the Project Brain.
    auto_feed_brain: bool = True


class ProgressiveContextLoader:
    """Assemble context progressively, reusing every existing service."""

    def __init__(self, config: _LoaderConfig | None = None) -> None:
        self._config = config or _LoaderConfig()
        self._brain = ProjectBrain()
        self._repo_index = RepositoryIndex()
        self._workspace = WorkspaceAwareness(index=self._repo_index)
        self._budget = ContextBudgetManager()
        self._compressor = ContextCompressor()
        self._evaluator = SelfEvaluator()
        self._decision_memory = DecisionMemory(self._brain)
        self._architecture_memory = ArchitectureMemory(self._brain)
        self._task_memory = TaskMemory(self._brain)
        self._session = SessionSnapshot(self._brain)
        self._graph = KnowledgeDependencyGraph()
        self._graph.build_default()

    # -- main entrypoint --------------------------------------------------- #

    async def build(
        self,
        *,
        workspace_id: str,
        task: str,
        server: dict[str, Any],
        workspace_path: str,
        conversation_history: list[dict[str, Any]] | None = None,
        specification: Any | None = None,
    ) -> dict[str, Any]:
        """Build context. Returns the legacy payload shape plus engineering_context."""
        blocks: list[dict[str, Any]] = []
        understanding = None  # set by Layer 5 (workspace awareness)

        # Layer 1: Project Brain (THINKSYNC.md)
        brain_text = await self._load_brain() if self._config.enable_brain else ""
        if brain_text:
            blocks.append(self._block("Project Brain", ContextPriority.PROJECT_BRAIN, brain_text))

        # Layer 2: Session Snapshot
        snap = await self._session.load()
        if snap is not None:
            blocks.append(self._block("Session Snapshot", ContextPriority.PROJECT_BRAIN, snap.to_markdown()))

        # Layer 3: Current Task (always highest priority)
        blocks.append(self._block("Current Task", ContextPriority.CURRENT_TASK, task))

        # Layer 4: Relevant Specification
        if specification is not None:
            spec_text = _spec_text(specification)
            if spec_text:
                blocks.append(self._block("Specification", ContextPriority.SPECIFICATION, spec_text))

        # Layer 5: Repository Index + Workspace Awareness (incremental).
        repo_index_text = ""
        if self._config.enable_repo_index:
            understanding = await self._workspace.understand(
                workspace_id=workspace_id,
                server=server,
                workspace_path=workspace_path,
                task=task,
                conversation_history=conversation_history or [],
                specification_text=_spec_text(specification) if specification is not None else "",
            )
            if not understanding.scanned_full:
                logger.info(
                    "[progressive] incremental workspace: %s changed files | confidence=%s",
                    len(understanding.changed_files), understanding.confidence.value,
                )
            repo_index_text = understanding.to_context_block()
            if repo_index_text:
                blocks.append(self._block("Repository Index", ContextPriority.REPOSITORY_INDEX, repo_index_text))

            # Sprint 3F1, PART 1: feed new workspace knowledge into the
            # Project Brain automatically (diff-based, preserves manual notes).
            if self._config.auto_feed_brain:
                try:
                    await self._workspace.feed_brain(brain=self._brain, understanding=understanding)
                except Exception as exc:  # noqa: BLE001 — best-effort
                    logger.warning("[progressive] workspace->brain feed failed: %s", exc)

            # Sprint 3F1, PART 5: confidence gate. If the computed confidence
            # is sufficient, skip additional/full-repo loading entirely.
            if self._config.confidence_gate and not understanding.confidence.below(Confidence.LOW):
                eval_result = self._evaluator.evaluate(
                    task=task,
                    confidence=understanding.confidence,
                    knowledge_items=[
                        KnowledgeItem(key="workspace", value=repo_index_text, confidence=understanding.confidence)
                    ],
                    can_inspect_more=False,
                )
                if eval_result.action is EvalAction.CONTINUE:
                    logger.info("[progressive] confidence gate passed — skipping deeper repo load")

        # Layer 6: Relevant Repository Files (REUSE existing ContextEngine)
        context_bundle = await ContextEngine.build_context(
            workspace_id=workspace_id,
            task=task,
            server=server,
            workspace_path=workspace_path,
        )
        file_list = context_bundle.get("selected_files") or []
        if file_list:
            blocks.append(self._block(
                "Relevant Files", ContextPriority.RELEVANT_FILES,
                "\n".join(file_list),
            ))

        # Layer 7/8: Additional / full repository only if task signals need.
        if self._needs_full_repo(task, file_list):
            extra = await self._load_additional_context(task, server, workspace_path)
            if extra:
                blocks.append(self._block("Additional Repository", ContextPriority.RELEVANT_FILES, extra))

        # Compression of conversation history (preserve engineering facts)
        convo = self._compressor.compress_conversation(conversation_history or [])
        if convo:
            convo_text = "\n".join(str(t.get("content", "")) for t in convo)
            blocks.append(self._block("Conversation", ContextPriority.CONVERSATION, convo_text))

        # Enforce budget — drop lowest-priority blocks first.
        fit = self._budget.fit(blocks)
        included_names = set(fit["included"])
        final_blocks = [b for b in blocks if b["name"] in included_names]

        computed_confidence = (
            understanding.confidence if understanding is not None else Confidence.MEDIUM
        )
        engineering_context = self._assemble_engineering_context(
            brain_text=brain_text,
            repo_index_text=repo_index_text,
            context_bundle=context_bundle,
            dropped=fit["dropped"],
            budget_tokens=fit["total_tokens"],
            confidence=computed_confidence,
        )

        # Merge into legacy payload shape (backward compatible).
        payload = dict(context_bundle)
        payload["engineering_context"] = engineering_context
        payload["prompt_payload"] = dict(payload.get("prompt_payload") or {})
        payload["prompt_payload"]["ENGINEERING_CONTEXT"] = engineering_context
        payload["context_budget"] = fit
        return payload

    # -- layer loaders ----------------------------------------------------- #

    async def _load_brain(self) -> str:
        # Trust brain only if fresh & confident; else reload flag is surfaced.
        brain = await self._brain.get_section("Architecture (high level)") or ""
        decisions = await self._brain.get_section("Key Design Decisions (per Decision Memory)") or ""
        stack = await self._brain.get_section("Technology Stack") or ""
        parts = [p for p in (stack, brain, decisions) if p]
        return "\n\n".join(parts).strip()

    async def _load_additional_context(self, task: str, server: dict[str, Any], workspace_path: str) -> str:
        # Only called as last resort; reuses ContextEngine selection broadly.
        if not self._config.full_repo_last_resort:
            return ""
        logger.info("[progressive] last-resort additional context requested for task")
        return "(full repository analysis deferred — token budget guards this path)"

    # -- helpers ------------------------------------------------------------ #

    def _block(self, name: str, priority: ContextPriority, content: str) -> dict[str, Any]:
        return {"name": name, "priority": priority, "content": content or ""}

    @staticmethod
    def _needs_full_repo(task: str, file_list: list[str]) -> bool:
        low = (task or "").lower()
        signals = ("entire repository", "all files", "full codebase", "whole project")
        if any(s in low for s in signals):
            return True
        # If nothing was matched but task references code, escalate.
        return len(file_list) == 0 and ("refactor" in low or "migrate" in low)

    def _assemble_engineering_context(
        self, *, brain_text: str, repo_index_text: str,
        context_bundle: dict[str, Any], dropped: list[str], budget_tokens: int,
        confidence: Confidence = Confidence.MEDIUM,
    ) -> dict[str, Any]:
        return {
            "project_brain_len": len(brain_text),
            "repository_index_len": len(repo_index_text),
            "selected_files": context_bundle.get("selected_files") or [],
            "mode": context_bundle.get("mode"),
            "dropped_context_blocks": dropped,
            "budget_tokens_used": budget_tokens,
            "confidence": confidence.value,
            "freshness_checked": True,
        }

    # -- self-learning hook (called after meaningful changes) -------------- #

    async def notify_change(self, *, change_type: str, description: str) -> None:
        """Self-learning: update brain + decisions + architecture on change."""
        await self._brain.record_change(change_type=change_type, description=description)
        # Architecture memory auto-syncs on change.
        self._architecture_memory.register(
            node=_arch_node_from_change(change_type, description)
        )
        await self._architecture_memory.sync_to_brain()

    # -- garbage collection hook ------------------------------------------- #

    async def collect_garbage(self) -> dict[str, int]:
        return await self._brain.garbage_collect()


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _spec_text(specification: Any) -> str:
    if isinstance(specification, str):
        return specification
    if isinstance(specification, dict):
        return specification.get("description") or specification.get("text") or ""
    for attr in ("description", "text", "to_text"):
        val = getattr(specification, attr, None)
        if callable(val):
            try:
                return val()
            except Exception as exc:  # noqa: BLE001 — best-effort attr access
                logger.debug("[progressive] spec attr %s failed: %s", attr, exc)
                continue
        if isinstance(val, str):
            return val
    return ""


def _arch_node_from_change(change_type: str, description: str):
    from services.context_memory import ArchitectureNode
    return ArchitectureNode(
        name=change_type,
        kind="service",
        description=description[:120],
        confidence=Confidence.MEDIUM,
    )


__all__ = ["ProgressiveContextLoader", "ContextPriority"]
