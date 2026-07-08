"""Resume Manager — Sprint 3.

Restores exact execution state when resuming a paused job.

Reads ``ExecutionCursor`` from ``jobs.execution_cursor`` and rebuilds
the runtime context so execution continues from exactly the right step
without restarting the job, rebuilding the plan, or redoing discovery.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from models.approval import ExecutionCursor, JobInteractionState, JobState
from models.job import JobStatus

logger = logging.getLogger(__name__)


class ExecutionCursorConflictError(Exception):
    """Raised when optimistic locking fails on ``ExecutionCursor`` update."""


# ---------------------------------------------------------------------------
# ResumeManager
# ---------------------------------------------------------------------------


class ResumeManager:
    """Restore execution state for a paused job.

    Responsibilities:
      - Load ``ExecutionCursor`` from DB
      - Validate that the job is in a resumable state
      - Restore planner state, workspace snapshot, pending steps
      - Return a resume bundle that ``agent_service.py`` passes to the
        execution loop
      """

    # - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - -
    # Public API
    # - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - -

    @classmethod
    async def load_resume_bundle(
        cls,
        job_id: str,
        conversation_id: str,
    ) -> dict[str, Any]:
        """Load the full resume bundle for a job.

        Raises ``ApprovedPlanViolationError`` if ``spec`` is frozen
        (Task 2 — global approved-plan immutability).

        Returns a dict with:
          - ``execution_cursor``: ``ExecutionCursor``
          - ``interaction_state``: ``JobInteractionState``
          - ``resume_point``: int (next step index)
          - ``pending_steps``: list[dict] (steps not yet executed)
          - ``planner_state``: dict (LLM context for planner)
          - ``workspace_snapshot``: dict (workspace state)
          - ``spec``: dict | None (frozen specification)
        """
        from core.database import get_supabase
        from models.approval import ensure_approved_plan_immutable

        # Load job row
        result = (
            get_supabase()
            .table("jobs")
            .select(
                "execution_cursor",
                "interaction_state",
                "spec",
                "plan",
                "status",
            )
            .eq("id", job_id)
            .eq("conversation_id", conversation_id)
            .limit(1)
            .execute()
        )
        if not result.data:
            raise ValueError(f"Job {job_id} not found")

        row = result.data[0]
        status = row.get("status")

        # Validate resumable state
        if status not in {
            JobStatus.WAITING_FOR_USER.value,
            JobStatus.PAUSED.value,
            JobStatus.APPROVED.value,
        }:
            raise ValueError(
                f"Job {job_id} is not resumable (status={status})"
            )

        # Parse execution cursor
        cursor = None
        if row.get("execution_cursor"):
            cursor = ExecutionCursor(**row["execution_cursor"])

        # Parse interaction state
        interaction = None
        if row.get("interaction_state"):
            interaction = JobInteractionState(
                **row["interaction_state"]
            )

        # Determine resume point
        resume_point = cursor.resume_point if cursor else 0

        # Rebuild pending steps (steps from resume_point onward)
        plan = row.get("plan") or []
        pending_steps = (
            plan[resume_point:] if resume_point < len(plan) else []
        )

        # Planner state (restored from cursor or default)
        planner_state = (
            cursor.planner_state if cursor else {}
        )

        # Workspace snapshot
        workspace_snapshot = (
            cursor.workspace_snapshot if cursor else {}
        )

        # Frozen spec
        spec = row.get("spec")

        # Task 2: enforce immutability — raise before returning bundle
        ensure_approved_plan_immutable(spec, context="resume_manager")

        logger.info(
            "[resume] job %s: resume_point=%s pending_steps=%s",
            job_id,
            resume_point,
            len(pending_steps),
        )

        return {
            "execution_cursor": cursor,
            "interaction_state": interaction,
            "resume_point": resume_point,
            "pending_steps": pending_steps,
            "planner_state": planner_state,
            "workspace_snapshot": workspace_snapshot,
            "spec": spec,
            "plan": plan,
        }

    @classmethod
    async def save_execution_cursor(
        cls,
        job_id: str,
        cursor: ExecutionCursor,
        *,
        expected_version: int | None = None,
    ) -> None:
        """Persist ``ExecutionCursor`` to ``jobs.execution_cursor``.

        With ``expected_version`` (optimistic locking):
          - updates succeed ONLY if stored ``cursor_version`` == ``expected_version``
          - on conflict, raises ``ExecutionCursorConflictError``
        """
        from core.database import get_supabase

        try:
            patch: dict[str, Any] = {
                "execution_cursor": cursor.model_dump(mode="json"),
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
            query = get_supabase().table("jobs").update(patch).eq("id", job_id)
            # Optimistic locking: only update if version matches
            if expected_version is not None:
                query = query.eq("cursor_version", expected_version)
            result = query.execute()
            if expected_version is not None and (not result.data or len(result.data) == 0):
                raise ExecutionCursorConflictError(
                    f"Cursor version conflict for job {job_id}: "
                    f"expected={expected_version}, stored version changed"
                )
        except ExecutionCursorConflictError:
            raise
        except Exception as exc:
            logger.error("[resume] failed to save execution cursor: %s", exc)
            raise

    @classmethod
    async def mark_step_completed(
        cls,
        job_id: str,
        step_index: int,
    ) -> None:
        """Mark a step as completed in the execution cursor."""
        from core.database import get_supabase

        # Load current cursor
        result = (
            get_supabase()
            .table("jobs")
            .select("execution_cursor")
            .eq("id", job_id)
            .limit(1)
            .execute()
        )
        if not result.data:
            return

        cursor_dict = result.data[0].get("execution_cursor") or {}
        cursor = ExecutionCursor(**cursor_dict)
        cursor.mark_step_completed(step_index)

        await cls.save_execution_cursor(job_id, cursor)

    @classmethod
    async def transition_to_running(
        cls,
        job_id: str,
    ) -> None:
        """Transition job status from WAITING/APPROVED to RUNNING."""
        from core.database import get_supabase

        try:
            get_supabase().table("jobs").update(
                {
                    "status": JobStatus.RUNNING.value,
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                }
            ).eq("id", job_id).execute()
        except Exception as exc:
            logger.warning("[resume] failed to transition to running: %s", exc)
