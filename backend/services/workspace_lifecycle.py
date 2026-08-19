"""Workspace Lifecycle Orchestrator (Part 4).

This is the SINGLE entry point for every workspace lifecycle transition. A
workspace lifecycle change is NOT plain CRUD: each transition runs through one
standard pipeline —

    validate → execute → cleanup → audit → cache invalidation
             ↳ (on failure) → compensation (reverse order)

COMPENSATION MODEL (approved architecture — the ONE mechanism for the whole
lifecycle; Part 5 CREATE saga builds on THIS, it does not replace it):

  * CompensationStep: a structured unit with ``name``, ``execute``,
    ``compensate`` and ``metadata``.
  * StepResult: the deterministic outcome of running a step — ``success``,
    ``started_at``, ``finished_at``, ``duration``, ``metadata``, ``error``.
  * CompensationLedger: collects steps in execution order and their
    StepResults. On failure it runs ``compensate`` for successfully-executed
    steps in REVERSE order.

Determinism guarantees:
  * ``execute`` runs exactly once per step.
  * ``compensate`` is invoked ONLY for steps whose ``execute`` succeeded.
  * A compensation failure NEVER masks the primary error: it is recorded
    separately, and the original failure is re-raised.

Part 5 (CREATE saga) and Part 7 (observability/audit) consume StepResult and
step metadata. No new compensation mechanism will be introduced later.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Awaitable, Callable, Optional

logger = logging.getLogger(__name__)


async def _audit_saga_step(*, step_name: str, phase: str, status: str,
                           duration_ms: Optional[int] = None, error: Optional[str] = None,
                           metadata: Optional[dict[str, Any]] = None) -> None:
    """Fire-and-forget structured audit for a saga compensation-step event (Part 7)."""
    try:
        from services.github_audit import AuditEvent, record_github_event

        await record_github_event(
            AuditEvent(
                event_type="saga.step",
                status=status,
                step_name=step_name,
                duration_ms=duration_ms,
                metadata={"phase": phase, "error": error, **(metadata or {})},
            )
        )
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------------------
# Structured step + result
# ---------------------------------------------------------------------------


@dataclass
class StepResult:
    """Deterministic outcome of executing (or compensating) a step."""

    name: str
    success: bool = False
    started_at: Optional[float] = None
    finished_at: Optional[float] = None
    metadata: dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None

    @property
    def duration(self) -> Optional[float]:
        if self.started_at is None or self.finished_at is None:
            return None
        return self.finished_at - self.started_at

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "success": self.success,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration": self.duration,
            "metadata": self.metadata,
            "error": self.error,
        }


# An async operation. execute/compensate take no args; they close over context.
AsyncOp = Callable[[], Awaitable[Any]]


@dataclass
class CompensationStep:
    """A structured lifecycle step with its own inverse (compensation).

    ``execute`` performs the forward action. ``compensate`` (optional) undoes it
    and is only ever called if ``execute`` succeeded. ``metadata`` is carried
    into the StepResult for audit/observability (Part 7) and saga use (Part 5).
    """

    name: str
    execute: AsyncOp
    compensate: Optional[AsyncOp] = None
    metadata: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Ledger
# ---------------------------------------------------------------------------


class CompensationError(Exception):
    """Raised only to annotate logs; never replaces the primary error."""


class CompensationLedger:
    """Runs steps in order; on failure compensates completed steps in reverse.

    This is the single compensation mechanism for the entire workspace
    lifecycle. Part 5's CREATE saga uses this exact ledger.
    """

    def __init__(self) -> None:
        self._executed: list[CompensationStep] = []
        self.results: list[StepResult] = []
        self.compensation_results: list[StepResult] = []

    async def run(self, step: CompensationStep) -> Any:
        """Execute a single step, recording a StepResult. Raises on failure
        AFTER compensating already-executed steps (reverse order)."""
        result = StepResult(name=step.name, metadata=dict(step.metadata))
        result.started_at = time.time()
        await _audit_saga_step(step_name=step.name, phase="execute_started", status="started",
                               metadata=step.metadata)
        try:
            value = await step.execute()
            result.finished_at = time.time()
            result.success = True
            if isinstance(value, dict):
                result.metadata.setdefault("return", value)
            self.results.append(result)
            # Only steps that executed successfully are eligible for compensation.
            self._executed.append(step)
            await _audit_saga_step(step_name=step.name, phase="execute_finished", status="ok",
                                   duration_ms=int((result.duration or 0) * 1000),
                                   metadata=step.metadata)
            logger.info(
                "[lifecycle] step ok | name=%s | duration=%.3fs",
                step.name, result.duration or 0.0,
            )
            return value
        except Exception as exc:  # noqa: BLE001
            result.finished_at = time.time()
            result.success = False
            result.error = f"{type(exc).__name__}: {exc}"
            self.results.append(result)
            await _audit_saga_step(step_name=step.name, phase="execute_finished", status="failed",
                                   duration_ms=int((result.duration or 0) * 1000),
                                   error=str(exc)[:400], metadata=step.metadata)
            logger.error("[lifecycle] step failed | name=%s | error=%s", step.name, result.error)
            # Primary failure: compensate what we've done, then re-raise the
            # ORIGINAL error (compensation errors never mask it).
            await self._compensate()
            raise

    async def _compensate(self) -> None:
        """Run compensation for executed steps in reverse order (best-effort)."""
        for step in reversed(self._executed):
            if step.compensate is None:
                continue
            comp = StepResult(name=f"compensate:{step.name}", metadata=dict(step.metadata))
            comp.started_at = time.time()
            await _audit_saga_step(step_name=f"compensate:{step.name}", phase="rollback_started",
                                   status="started", metadata=step.metadata)
            try:
                await step.compensate()
                comp.finished_at = time.time()
                comp.success = True
                await _audit_saga_step(step_name=f"compensate:{step.name}", phase="rollback_finished",
                                       status="ok",
                                       duration_ms=int((comp.duration or 0) * 1000),
                                       metadata=step.metadata)
                logger.info("[lifecycle] compensated | name=%s", step.name)
            except Exception as cexc:  # noqa: BLE001
                comp.finished_at = time.time()
                comp.success = False
                comp.error = f"{type(cexc).__name__}: {cexc}"
                await _audit_saga_step(step_name=f"compensate:{step.name}", phase="rollback_finished",
                                       status="failed",
                                       duration_ms=int((comp.duration or 0) * 1000),
                                       error=str(cexc)[:400], metadata=step.metadata)
                # Record separately; do NOT raise — primary error must survive.
                logger.error(
                    "[lifecycle] compensation FAILED | name=%s | error=%s (primary error preserved)",
                    step.name, comp.error,
                )
            self.compensation_results.append(comp)


# ---------------------------------------------------------------------------
# Transition actions + context/result
# ---------------------------------------------------------------------------


class LifecycleAction(str, Enum):
    DELETE = "delete"
    DISCONNECT = "disconnect"


@dataclass
class LifecycleContext:
    action: LifecycleAction
    workspace_id: str
    user_id: str
    # Resolved during validate():
    workspace: Optional[dict[str, Any]] = None
    server: Optional[dict[str, Any]] = None
    connection: Optional[dict[str, Any]] = None


@dataclass
class LifecycleResult:
    action: str
    workspace_id: str
    success: bool
    steps: list[dict[str, Any]] = field(default_factory=list)
    compensations: list[dict[str, Any]] = field(default_factory=list)
    detail: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Orchestrator — the SINGLE entry point for every lifecycle transition
# ---------------------------------------------------------------------------


class WorkspaceLifecycleOrchestrator:
    """Single entry point for workspace lifecycle transitions.

    Every transition runs the same pipeline: validate → execute (via the
    compensation ledger) → cleanup → audit → cache invalidation. On any
    execute failure the ledger compensates completed steps in reverse and the
    original error is re-raised.
    """

    @staticmethod
    async def transition(*, ctx: LifecycleContext) -> LifecycleResult:
        # 1) VALIDATE (ownership + existence + resolve related rows).
        await WorkspaceLifecycleOrchestrator._validate(ctx)

        ledger = CompensationLedger()
        try:
            if ctx.action is LifecycleAction.DELETE:
                await WorkspaceLifecycleOrchestrator._execute_delete(ctx, ledger)
            elif ctx.action is LifecycleAction.DISCONNECT:
                await WorkspaceLifecycleOrchestrator._execute_disconnect(ctx, ledger)
            else:  # pragma: no cover - guarded by enum
                raise ValueError(f"Unsupported lifecycle action: {ctx.action}")
        except Exception:
            # Audit the failed transition (compensation already ran in ledger).
            await WorkspaceLifecycleOrchestrator._audit(
                ctx, success=False, ledger=ledger
            )
            raise

        # 4) AUDIT + 5) CACHE INVALIDATION happen on success.
        await WorkspaceLifecycleOrchestrator._audit(ctx, success=True, ledger=ledger)
        await WorkspaceLifecycleOrchestrator._invalidate_cache(ctx)

        return LifecycleResult(
            action=ctx.action.value,
            workspace_id=ctx.workspace_id,
            success=True,
            steps=[r.as_dict() for r in ledger.results],
            compensations=[r.as_dict() for r in ledger.compensation_results],
        )

    # ---------------------------------------------------------------- validate
    @staticmethod
    async def _validate(ctx: LifecycleContext) -> None:
        from services.workspace_service import WorkspaceService

        # Raises 404 if not owned/found — ownership enforced here.
        workspace = WorkspaceService.get_workspace_by_id(
            id=ctx.workspace_id, user_id=ctx.user_id
        )
        ctx.workspace = workspace

        # Resolve the linked GitHub connection (if any) for cleanup decisions.
        conn_id = workspace.get("github_connection_id")
        if conn_id:
            supabase = await _supa()
            res = (
                await supabase.table("github_connections")
                .select("*")
                .eq("id", conn_id)
                .maybe_single()
                .execute()
            )
            ctx.connection = (res.data if res else None) or None

    # ----------------------------------------------------------------- delete
    @staticmethod
    async def _execute_delete(ctx: LifecycleContext, ledger: CompensationLedger) -> None:
        from services.workspace_service import WorkspaceService

        workspace = ctx.workspace or {}
        conn = ctx.connection
        supabase = await _supa()

        # Step 1: delete the workspace DB row (compensation: reinsert it).
        original_row = dict(workspace)

        async def _del_row() -> Any:
            await supabase.table("workspaces").delete().eq("id", ctx.workspace_id).eq(
                "user_id", ctx.user_id
            ).execute()
            return {"deleted": ctx.workspace_id}

        async def _restore_row() -> Any:
            # Reinsert the exact prior row to undo the delete.
            restore = {k: v for k, v in original_row.items() if k != "url"}
            await supabase.table("workspaces").insert(restore).execute()
            return {"restored": ctx.workspace_id}

        await ledger.run(
            CompensationStep(
                name="delete_workspace_row",
                execute=_del_row,
                compensate=_restore_row,
                metadata={"workspace_id": ctx.workspace_id},
            )
        )

        # Step 2: remove the remote workspace directory (no compensation —
        # filesystem removal is terminal; guarded by a strict path check).
        remote_path = str(workspace.get("path") or "")

        async def _rm_remote() -> Any:
            _assert_safe_workspace_path(remote_path)
            server = ctx.server or await _resolve_server(ctx)
            if server is not None:
                from services.ssh_service import SSHService
                import shlex

                await SSHService.execute(
                    server=server, command=f"rm -rf {shlex.quote(remote_path)}"
                )
            return {"removed_path": remote_path}

        await ledger.run(
            CompensationStep(
                name="remove_remote_dir",
                execute=_rm_remote,
                compensate=None,
                metadata={"path": remote_path},
            )
        )

        # Step 3: cleanup an App connection that ONLY this workspace referenced
        # (orphan prevention). SSH connections are user-managed; leave them.
        if conn and conn.get("auth_method") == "app":
            conn_id = conn.get("id")

            async def _del_conn() -> Any:
                # Only delete if no other workspace still references it.
                others = (
                    await supabase.table("workspaces")
                    .select("id")
                    .eq("github_connection_id", conn_id)
                    .execute()
                )
                if others and others.data:
                    return {"kept_connection": conn_id, "still_referenced": len(others.data)}
                await supabase.table("github_connections").delete().eq("id", conn_id).execute()
                return {"deleted_connection": conn_id}

            await ledger.run(
                CompensationStep(
                    name="cleanup_app_connection",
                    execute=_del_conn,
                    compensate=None,  # best-effort orphan cleanup
                    metadata={"connection_id": conn_id},
                )
            )

    # ------------------------------------------------------------- disconnect
    @staticmethod
    async def _execute_disconnect(ctx: LifecycleContext, ledger: CompensationLedger) -> None:
        workspace = ctx.workspace or {}
        conn = ctx.connection
        supabase = await _supa()
        prior_conn_id = workspace.get("github_connection_id")

        # Step 1: unlink the connection from the workspace (-> becomes ThinkSync).
        async def _unlink() -> Any:
            await supabase.table("workspaces").update(
                {"github_connection_id": None}
            ).eq("id", ctx.workspace_id).eq("user_id", ctx.user_id).execute()
            return {"unlinked": prior_conn_id}

        async def _relink() -> Any:
            await supabase.table("workspaces").update(
                {"github_connection_id": prior_conn_id}
            ).eq("id", ctx.workspace_id).eq("user_id", ctx.user_id).execute()
            return {"relinked": prior_conn_id}

        await ledger.run(
            CompensationStep(
                name="unlink_connection",
                execute=_unlink,
                compensate=_relink,
                metadata={"workspace_id": ctx.workspace_id, "connection_id": prior_conn_id},
            )
        )

        # Step 2: cleanup the App connection if now orphaned.
        if conn and conn.get("auth_method") == "app":
            conn_id = conn.get("id")

            async def _del_conn() -> Any:
                others = (
                    await supabase.table("workspaces")
                    .select("id")
                    .eq("github_connection_id", conn_id)
                    .execute()
                )
                if others and others.data:
                    return {"kept_connection": conn_id}
                await supabase.table("github_connections").delete().eq("id", conn_id).execute()
                return {"deleted_connection": conn_id}

            await ledger.run(
                CompensationStep(
                    name="cleanup_app_connection",
                    execute=_del_conn,
                    compensate=None,
                    metadata={"connection_id": conn_id},
                )
            )

    # -------------------------------------------------------------- audit/cache
    @staticmethod
    async def _audit(ctx: LifecycleContext, *, success: bool, ledger: CompensationLedger) -> None:
        from services.github_audit import AuditEvent, record_github_event

        conn = ctx.connection or {}
        await record_github_event(
            AuditEvent(
                event_type=f"workspace.{ctx.action.value}.{'ok' if success else 'failed'}",
                installation_id=conn.get("installation_id"),
                workspace_id=ctx.workspace_id,
                github_connection_id=conn.get("id"),
                server_id=(ctx.server or {}).get("id"),
                user_id=ctx.user_id,
                repo_id=conn.get("repo_id"),
                repo_full_name=conn.get("repo_full_name"),
                status="ok" if success else "failed",
                step_name=f"lifecycle.{ctx.action.value}",
                metadata={
                    "workspace_id": ctx.workspace_id,
                    "steps": [r.as_dict() for r in ledger.results],
                    "compensations": [r.as_dict() for r in ledger.compensation_results],
                },
            )
        )

    @staticmethod
    async def _invalidate_cache(ctx: LifecycleContext) -> None:
        conn = ctx.connection or {}
        installation_id = conn.get("installation_id")
        if installation_id:
            from services.github_app_service import invalidate_installation_token

            await invalidate_installation_token(str(installation_id))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _supa():
    from core.database import get_supabase_async

    return await get_supabase_async()


async def _resolve_server(ctx: LifecycleContext) -> Optional[dict[str, Any]]:
    from services.server_service import ServerService

    workspace = ctx.workspace or {}
    server_id = workspace.get("server_id")
    if not server_id:
        return None
    try:
        server = ServerService.get_server(server_id=server_id, user_id=ctx.user_id)
        ctx.server = server
        return server
    except Exception:  # noqa: BLE001
        return None


def _assert_safe_workspace_path(path: str) -> None:
    """Guard against destructive rm -rf on an unexpected path.

    The path MUST live under the workspaces root and MUST NOT be the root
    itself. The path comes from the DB (never user free-text), but we validate
    defensively because the operation is irreversible.
    """
    from services.workspace_service import WorkspaceService

    root = WorkspaceService._workspaces_root().rstrip("/")
    normalized = (path or "").rstrip("/")
    if not normalized or normalized == root:
        raise ValueError(f"Refusing to remove unsafe workspace path: {path!r}")
    if not normalized.startswith(root + "/"):
        raise ValueError(f"Workspace path escapes workspaces root: {path!r}")
    if ".." in normalized.split("/"):
        raise ValueError(f"Workspace path contains traversal: {path!r}")

