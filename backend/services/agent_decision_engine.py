"""ThinkSync Agent Decision Engine — SHADOW MODE (Sprint 4C).

This module implements the *pure* Decision Engine designed in Sprint 4B. It is
an **observation-only** orchestration component:

    * It does NOT execute anything.
    * It does NOT call tools, the LLM, the database, Redis, SSH, or the network.
    * It does NOT write files or mutate any argument.
    * It only DECIDES *what should happen next* from a snapshot of already-
      collected signals and returns one structured :class:`Decision`.

The live production pipeline (``run_agent_pipeline``) remains authoritative.
During Sprint 4C the engine runs in parallel behind the ``DECISION_ENGINE_SHADOW``
feature flag and its output is only compared/recorded (see
:func:`record_shadow_comparison`). No production behavior changes.

Design constraints honoured (Sprint 4C STRICT RULES):
    * Pure / deterministic / stateless / side-effect free.
    * Consumes the EXISTING authoritative understanding (Requirement Discovery,
      Project Specification, Conversation, Memory, Implementation Intelligence,
      Progressive Context, Adaptive Clarification, Approval/Resume/Event state,
      Intent confidence, History, current execution state). It does NOT
      introduce Goal / Objective / Mission / Task-State or any new orchestration
      layer, and it does NOT replace those subsystems — it reads them.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Any


# ---------------------------------------------------------------------------
# Decision vocabulary
# ---------------------------------------------------------------------------
# NOTE: ``ExecutionKind`` intentionally mirrors the EXISTING intent domain
# ("chat" / "code" / "server") so the shadow comparison against the current
# pipeline's ``intent`` branch is apples-to-apples. It is NOT a new concept —
# it is the same three execution kinds the pipeline already dispatches on.
class ExecutionKind(str, Enum):
    CHAT = "chat"
    CODE = "code"
    SERVER = "server"
    NONE = "none"  # e.g. suspended for clarification/approval before any exec


class NextAction(str, Enum):
    """The single next step the engine believes the orchestrator should take.

    These map onto decision-graph nodes from Sprint 4B. They are advisory in
    shadow mode.
    """

    CLARIFY = "clarify"
    DISCOVER = "discover"
    LOAD_CONTEXT = "load_context"
    PLAN = "plan"
    APPROVE = "approve"
    EXECUTE = "execute"
    VERIFY = "verify"
    RESUME = "resume"
    COMPLETE = "complete"


@dataclass(frozen=True)
class Decision:
    """One structured, immutable decision. No execution, no side effects."""

    next_action: NextAction
    execution_kind: ExecutionKind
    reason: str
    confidence: float
    required_modules: tuple[str, ...] = field(default_factory=tuple)
    approval_required: bool = False
    context_required: bool = False
    clarification_required: bool = False
    repository_required: bool = False
    specification_required: bool = False

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["next_action"] = self.next_action.value
        data["execution_kind"] = self.execution_kind.value
        data["required_modules"] = list(self.required_modules)
        return data


# ---------------------------------------------------------------------------
# Decision input snapshot
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class DecisionState:
    """Immutable snapshot of already-collected signals.

    Every field is optional so the caller can populate only what it has cheaply
    at the decision point. The engine never fetches anything itself.

    Fields deliberately reuse the EXISTING authoritative understanding — none of
    them introduce a new orchestration concept.
    """

    # Raw request + weighted intent signal (metadata, NOT the router).
    objective: str = ""
    intent: str | None = None                 # current pipeline's classified intent
    intent_confidence: float | None = None    # from classify_intent_with_confidence
    task_mode: str | None = None

    # Authoritative project understanding (Sprint 2 / 3).
    has_specification: bool = False
    specification_confidence: float | None = None
    specification_missing_info: int = 0
    needs_discovery: bool = False             # should_run_discovery() result

    # Conversation / memory / history.
    conversation_len: int = 0
    memory_len: int = 0
    has_history: bool = False

    # Workspace / repository / server.
    has_workspace: bool = False
    existing_workspace: bool = False
    has_repository_index: bool = False
    has_server: bool = False

    # Context / clarification signals.
    context_available: bool = False
    clarification_pending: bool = False
    clarification_questions: int = 0

    # Approval / resume / event state.
    approval_pending: bool = False
    is_resume: bool = False
    interaction_state: str | None = None      # e.g. "WAITING_FOR_USER"

    # Deployment heuristic already computed by the pipeline (regex on objective).
    deployment_signal: bool = False

    # Current execution state (so the engine can advise the *next* node).
    current_status: str | None = None
    previous_decisions: int = 0


# ---------------------------------------------------------------------------
# The engine
# ---------------------------------------------------------------------------
class AgentDecisionEngine:
    """Pure decision function. Stateless — all methods are static."""

    @staticmethod
    def decide(state: DecisionState) -> Decision:
        """Return ONE structured decision. Deterministic and side-effect free.

        The ordering mirrors the Sprint 4B Decision Graph:
        resume -> clarify -> discover -> context -> plan -> approve -> execute.
        Only signals present in ``state`` are considered; nothing is fetched.
        """
        exec_kind = AgentDecisionEngine._infer_execution_kind(state)

        # 0. Resume takes precedence — a suspended job that woke up continues.
        if state.is_resume or state.interaction_state == "WAITING_FOR_USER":
            return Decision(
                next_action=NextAction.RESUME,
                execution_kind=exec_kind,
                reason="Job is resuming from a suspended state.",
                confidence=0.95,
                required_modules=("ResumeManager", "EventWaitEngine"),
            )

        # 1. Clarification gate — do not proceed with open blocking questions.
        if state.clarification_pending and state.clarification_questions > 0:
            return Decision(
                next_action=NextAction.CLARIFY,
                execution_kind=ExecutionKind.NONE,
                reason=(
                    f"{state.clarification_questions} clarification question(s) "
                    "pending before execution."
                ),
                confidence=0.85,
                required_modules=("AdaptiveClarificationEngine", "ClarificationEngine"),
                clarification_required=True,
            )

        # 2. Discovery gate — build/complete the project specification first.
        if state.needs_discovery and not state.has_specification:
            return Decision(
                next_action=NextAction.DISCOVER,
                execution_kind=exec_kind,
                reason="Requirement discovery required to build a specification.",
                confidence=0.8,
                required_modules=("RequirementDiscovery",),
                specification_required=True,
            )

        # 3. Specification completeness — low-confidence spec loops to clarify.
        if (
            state.has_specification
            and state.specification_confidence is not None
            and state.specification_confidence < 0.5
            and state.specification_missing_info > 0
        ):
            return Decision(
                next_action=NextAction.CLARIFY,
                execution_kind=ExecutionKind.NONE,
                reason=(
                    "Specification confidence low "
                    f"({state.specification_confidence:.2f}) with "
                    f"{state.specification_missing_info} missing field(s)."
                ),
                confidence=0.7,
                required_modules=("AdaptiveClarificationEngine", "ProjectSpecification"),
                clarification_required=True,
                specification_required=True,
            )

        # 4. Context gate — code/server work needs workspace/repository context.
        needs_context = exec_kind in (ExecutionKind.CODE, ExecutionKind.SERVER)
        if needs_context and not state.context_available:
            return Decision(
                next_action=NextAction.LOAD_CONTEXT,
                execution_kind=exec_kind,
                reason="Execution context not yet loaded for a code/server task.",
                confidence=0.75,
                required_modules=("ProgressiveContext", "ContextEngine")
                + (("RepositoryIndex",) if state.has_repository_index else ()),
                context_required=True,
                repository_required=state.has_repository_index,
            )

        # 5. Planning gate — server execution is plan-driven.
        if exec_kind == ExecutionKind.SERVER:
            approval = AgentDecisionEngine._approval_needed(state, exec_kind)
            if state.current_status in (None, "queued", "running") and state.previous_decisions == 0:
                return Decision(
                    next_action=NextAction.PLAN,
                    execution_kind=ExecutionKind.SERVER,
                    reason="Server task requires a build plan before execution.",
                    confidence=0.8,
                    required_modules=("Planner", "ImplementationIntelligence"),
                    approval_required=approval,
                    context_required=True,
                )

        # 6. Approval gate — a risky action must be approved before it runs.
        if AgentDecisionEngine._approval_needed(state, exec_kind):
            return Decision(
                next_action=NextAction.APPROVE,
                execution_kind=exec_kind,
                reason="A risky action requires approval before execution.",
                confidence=0.7,
                required_modules=("ApprovalPolicyEngine", "ApprovalEngine"),
                approval_required=True,
            )

        # 7. Execute — chat/code/server dispatch.
        return Decision(
            next_action=NextAction.EXECUTE,
            execution_kind=exec_kind,
            reason=f"Ready to execute a {exec_kind.value} action.",
            confidence=AgentDecisionEngine._execution_confidence(state, exec_kind),
            required_modules=AgentDecisionEngine._execution_modules(exec_kind),
        )

    # ------------------------------------------------------------------
    # Signal-weighting helpers (intent is ONE weighted signal, not the router)
    # ------------------------------------------------------------------
    @staticmethod
    def _infer_execution_kind(state: DecisionState) -> ExecutionKind:
        """Blend signals into an execution kind.

        Intent contributes a weight; workspace/spec/deployment signals can
        adjust it. This deliberately mirrors the current pipeline's heuristics
        (deployment regex override, telegram/code re-route) so shadow
        comparison is meaningful, but expresses them as weighted signals rather
        than hardcoded routing.
        """
        intent = (state.intent or "").strip().lower()

        # Deployment signal is a strong push toward server (matches pipeline).
        if state.deployment_signal:
            # ...unless it is clearly a code/script authoring task on an
            # existing workspace (pipeline re-routes telegram/bot/code to code).
            if intent == "code":
                return ExecutionKind.CODE
            return ExecutionKind.SERVER

        if intent in ("chat", "code", "server"):
            return ExecutionKind(intent)

        # Unknown/empty intent: the pipeline coerces to "code".
        return ExecutionKind.CODE

    @staticmethod
    def _approval_needed(state: DecisionState, exec_kind: ExecutionKind) -> bool:
        if state.approval_pending:
            return True
        # Server/deployment writes are the canonical approval-worthy actions.
        return exec_kind == ExecutionKind.SERVER and state.deployment_signal

    @staticmethod
    def _execution_confidence(state: DecisionState, exec_kind: ExecutionKind) -> float:
        base = 0.6
        if state.intent_confidence is not None:
            base = max(base, min(1.0, float(state.intent_confidence)))
        if exec_kind == ExecutionKind.CHAT:
            base = max(base, 0.65)
        return round(base, 3)

    @staticmethod
    def _execution_modules(exec_kind: ExecutionKind) -> tuple[str, ...]:
        if exec_kind == ExecutionKind.CHAT:
            return ("generate_chat_response",)
        if exec_kind == ExecutionKind.CODE:
            return ("_run_code_execution", "ImplementationIntelligence")
        if exec_kind == ExecutionKind.SERVER:
            return ("run_server_execution", "executor")
        return tuple()

    @staticmethod
    def recommend(state: "DecisionState") -> "Recommendation":
        """Pure weighted recommendation built on top of ``decide()``.

        Recommend-only: never executes, no side effects, deterministic.
        """
        return _recommend(state)


# ===========================================================================
# Sprint 4D — WEIGHTED recommendation layer (still recommend-only, pure)
# ===========================================================================
class SafetyLevel(str, Enum):
    """How safe it is to *act* on a recommendation. Advisory only — the real
    security gates (permission/write-gate/approval/ownership) remain
    authoritative and are never replaced by this label."""

    SAFE = "safe"          # no write/side-effect implication
    GUARDED = "guarded"    # requires an existing gate to pass (approval/write)
    SENSITIVE = "sensitive"  # server/deploy write path — highest scrutiny


class ExecutionCategory(str, Enum):
    """Coarse category of the recommended action (for statistics/grouping)."""

    CONVERSATION = "conversation"
    CODE = "code"
    SERVER = "server"
    GATE = "gate"          # pre-execution gate: clarify/discover/context/plan/approve/resume


@dataclass(frozen=True)
class Recommendation:
    """A weighted, explainable recommendation. NEVER executes.

    Wraps the pure :class:`Decision` with the extra weighting fields the 4D
    brief requires: priority, evidence, required signals, safety level, and
    execution category. Immutable and deterministic.
    """

    decision: Decision
    confidence: float
    priority: int                       # 1 (highest) .. 5 (lowest)
    reason: str
    evidence: tuple[str, ...]
    required_signals: tuple[str, ...]
    safety_level: SafetyLevel
    execution_category: ExecutionCategory

    def to_dict(self) -> dict[str, Any]:
        return {
            "decision": self.decision.to_dict(),
            "confidence": self.confidence,
            "priority": self.priority,
            "reason": self.reason,
            "evidence": list(self.evidence),
            "required_signals": list(self.required_signals),
            "safety_level": self.safety_level.value,
            "execution_category": self.execution_category.value,
        }


# Extend the engine with a pure recommend() built on top of decide().
def _recommend(state: DecisionState) -> Recommendation:
    decision = AgentDecisionEngine.decide(state)

    # Execution category
    if decision.next_action != NextAction.EXECUTE or decision.execution_kind == ExecutionKind.NONE:
        category = ExecutionCategory.GATE
    elif decision.execution_kind == ExecutionKind.CHAT:
        category = ExecutionCategory.CONVERSATION
    elif decision.execution_kind == ExecutionKind.SERVER:
        category = ExecutionCategory.SERVER
    else:
        category = ExecutionCategory.CODE

    # Safety level — advisory scrutiny hint, NOT a security gate.
    if decision.execution_kind == ExecutionKind.SERVER or state.deployment_signal:
        safety = SafetyLevel.SENSITIVE
    elif decision.approval_required or category == ExecutionCategory.CODE:
        safety = SafetyLevel.GUARDED
    else:
        safety = SafetyLevel.SAFE

    # Priority — gates that block progress rank highest; execution is mid; chat lowest.
    priority_map = {
        NextAction.RESUME: 1,
        NextAction.CLARIFY: 1,
        NextAction.APPROVE: 1,
        NextAction.DISCOVER: 2,
        NextAction.LOAD_CONTEXT: 2,
        NextAction.PLAN: 2,
        NextAction.VERIFY: 3,
        NextAction.EXECUTE: 3,
        NextAction.COMPLETE: 4,
    }
    priority = priority_map.get(decision.next_action, 3)
    if decision.next_action == NextAction.EXECUTE and decision.execution_kind == ExecutionKind.CHAT:
        priority = 4

    # Evidence — the concrete signals that justified the decision (deterministic).
    evidence: list[str] = [f"next_action={decision.next_action.value}",
                           f"execution_kind={decision.execution_kind.value}"]
    if state.intent is not None:
        evidence.append(f"intent={state.intent}")
    if state.intent_confidence is not None:
        evidence.append(f"intent_confidence={state.intent_confidence}")
    if state.deployment_signal:
        evidence.append("deployment_signal=true")
    if state.clarification_pending:
        evidence.append(f"clarification_questions={state.clarification_questions}")
    if state.needs_discovery:
        evidence.append("needs_discovery=true")
    if state.has_specification:
        evidence.append(f"specification_confidence={state.specification_confidence}")
    if state.is_resume:
        evidence.append("is_resume=true")
    if state.context_available:
        evidence.append("context_available=true")

    # Required signals the caller should have populated for this decision.
    required_signals: list[str] = ["intent"]
    if decision.clarification_required:
        required_signals.append("clarification_pending")
    if decision.specification_required:
        required_signals.append("has_specification")
    if decision.context_required:
        required_signals.append("context_available")
    if decision.approval_required:
        required_signals.append("approval_pending")

    return Recommendation(
        decision=decision,
        confidence=decision.confidence,
        priority=priority,
        reason=decision.reason,
        evidence=tuple(evidence),
        required_signals=tuple(required_signals),
        safety_level=safety,
        execution_category=category,
    )


__all__ = [
    "AgentDecisionEngine",
    "Decision",
    "DecisionState",
    "NextAction",
    "ExecutionKind",
    "Recommendation",
    "SafetyLevel",
    "ExecutionCategory",
]
