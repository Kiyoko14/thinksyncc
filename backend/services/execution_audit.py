"""Execution audit and recovery support for durable job tracking.

This module provides comprehensive execution auditing, state transition tracking,
and recovery support for ThinkSync job execution.

Key responsibilities:
  1. Track every state transition with timestamp and reason.
  2. Record every execution event with sequencing.
  3. Support reconstruction of complete execution timeline.
  4. Identify orphaned/unfinished jobs for recovery.
  5. Provide audit queries for debugging and compliance.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

from core.database import get_supabase
from models.job import JobStatus

logger = logging.getLogger(__name__)


class ExecutionAudit:
    """Audit trail queries and recovery support."""

    @staticmethod
    def get_execution_timeline(job_id: str) -> dict[str, Any]:
        """Reconstruct complete execution timeline from audit tables.

        Returns:
            Dictionary containing:
              - job_id: job identifier
              - created_at: job creation timestamp
              - state_transitions: list of state changes with timestamps
              - events: chronological list of all execution events
              - status: current job status
              - can_reconstruct: whether full timeline is available

        Questions answered:
          - What happened?
          - In what order?
          - When did it happen?
        """
        try:
            job_result = (
                get_supabase().table("jobs").select("*").eq("id", job_id).maybe_single().execute()
            )
            if not job_result or not job_result.data:
                return {"error": "job_not_found", "job_id": job_id}

            job_data = job_result.data

            transitions_result = (
                get_supabase()
                .table("job_state_transitions")
                .select("*")
                .eq("job_id", job_id)
                .order("created_at", desc=False)
                .execute()
            )

            events_result = (
                get_supabase()
                .table("job_events")
                .select("*")
                .eq("job_id", job_id)
                .order("sequence", desc=False)
                .execute()
            )

            # Fetch new normalized tables for full timeline
            steps_result = (
                get_supabase()
                .table("job_steps")
                .select("*")
                .eq("job_id", job_id)
                .order("step_number", desc=False)
                .execute()
            )
            decisions_result = (
                get_supabase()
                .table("job_decisions")
                .select("*")
                .eq("job_id", job_id)
                .order("created_at", desc=False)
                .execute()
            )
            retries_result = (
                get_supabase()
                .table("job_retries")
                .select("*")
                .eq("job_id", job_id)
                .order("created_at", desc=False)
                .execute()
            )
            errors_result = (
                get_supabase()
                .table("job_execution_details")
                .select("*")
                .eq("job_id", job_id)
                .eq("detail_type", "error")
                .order("created_at", desc=False)
                .execute()
            )

            timeline = {
                "job_id": job_id,
                "status": job_data.get("status"),
                "created_at": job_data.get("created_at"),
                "updated_at": job_data.get("updated_at"),
                "objective": job_data.get("objective"),
                "state_transitions": transitions_result.data or [],
                "events": [
                    {
                        "sequence": e.get("sequence"),
                        "type": e.get("event_type"),
                        "timestamp": e.get("created_at"),
                        "payload": e.get("payload"),
                    }
                    for e in (events_result.data or [])
                ],
                "steps": steps_result.data or [],
                "decisions": decisions_result.data or [],
                "retries": retries_result.data or [],
                "errors": errors_result.data or [],
                "can_reconstruct": bool(
                    events_result.data
                    or steps_result.data
                    or decisions_result.data
                    or retries_result.data
                ),
                "event_count": len(events_result.data or []),
                "transition_count": len(transitions_result.data or []),
                "step_count": len(steps_result.data or []),
                "decision_count": len(decisions_result.data or []),
                "retry_count": len(retries_result.data or []),
                "error_count": len(errors_result.data or []),
            }
            return timeline
        except Exception as exc:
            logger.error("Failed to reconstruct execution timeline for job=%s: %s", job_id, exc)
            return {"error": "reconstruction_failed", "job_id": job_id, "detail": str(exc)}

    @staticmethod
    def get_state_transitions(job_id: str) -> list[dict[str, Any]]:
        """Retrieve all state transitions for a job.

        Returns list of state transitions in chronological order.
        Each transition includes from_status, to_status, step, tool, trace_id, and timestamp.
        """
        try:
            result = (
                get_supabase()
                .table("job_state_transitions")
                .select("*")
                .eq("job_id", job_id)
                .order("created_at", desc=False)
                .execute()
            )
            return result.data or []
        except Exception as exc:
            logger.warning("Failed to retrieve state transitions for job=%s: %s", job_id, exc)
            return []

    @staticmethod
    def find_orphaned_jobs(hours: int = 1) -> list[dict[str, Any]]:
        """Find jobs that were not completed within specified time window.

        These are candidates for recovery or cleanup.

        Args:
            hours: look for jobs updated more than N hours ago

        Returns:
            List of jobs with status in [queued, running, waiting_for_llm] that are stale.
        """
        try:
            cutoff_time = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
            result = (
                get_supabase()
                .table("jobs")
                .select("id,user_id,workspace_id,server_id,objective,status,created_at,updated_at")
                .in_("status", ["queued", "running", "waiting_for_llm"])
                .lt("updated_at", cutoff_time)
                .order("updated_at", desc=False)
                .execute()
            )
            return result.data or []
        except Exception as exc:
            logger.warning("Failed to find orphaned jobs: %s", exc)
            return []

    @staticmethod
    def find_jobs_missing_completion_event(limit: int = 100) -> list[dict[str, Any]]:
        """Find completed/failed jobs that are missing final completion event.

        These indicate incomplete audit trails and should be flagged.
        """
        try:
            result = (
                get_supabase()
                .table("jobs")
                .select("id,user_id,status,created_at,updated_at")
                .in_("status", ["completed", "failed"])
                .order("updated_at", desc=True)
                .limit(limit)
                .execute()
            )

            incomplete = []
            for job in result.data or []:
                job_id = job.get("id")
                events_result = (
                    get_supabase()
                    .table("job_events")
                    .select("id")
                    .eq("job_id", job_id)
                    .eq("event_type", "completed")
                    .maybe_single()
                    .execute()
                )
                if not events_result or not events_result.data:
                    incomplete.append(job)

            return incomplete
        except Exception as exc:
            logger.warning("Failed to find jobs with missing completion events: %s", exc)
            return []

    @staticmethod
    def mark_job_for_recovery(job_id: str, reason: str = "manual_recovery_request") -> bool:
        """Mark job as recoverable and set status for retry.

        Used when a job becomes orphaned and should be retried.
        """
        try:
            get_supabase().table("job_state_transitions").insert(
                {
                    "job_id": job_id,
                    "from_status": "running",
                    "to_status": "queued",
                    "step": None,
                    "tool": None,
                    "trace_id": None,
                    "reason": reason,
                }
            ).execute()
            get_supabase().table("jobs").update(
                {"status": JobStatus.QUEUED.value}
            ).eq("id", job_id).execute()
            return True
        except Exception as exc:
            logger.error("Failed to mark job for recovery (job=%s): %s", job_id, exc)
            return False

    @staticmethod
    def get_event_statistics(job_id: str) -> dict[str, Any]:
        """Retrieve event statistics for audit analysis.

        Returns:
            Dictionary with event type counts and execution characteristics.
        """
        try:
            events_result = (
                get_supabase()
                .table("job_events")
                .select("event_type")
                .eq("job_id", job_id)
                .execute()
            )

            stats: dict[str, int] = {}
            for event in events_result.data or []:
                event_type = event.get("event_type", "unknown")
                stats[event_type] = stats.get(event_type, 0) + 1

            transitions_result = (
                get_supabase()
                .table("job_state_transitions")
                .select("from_status,to_status")
                .eq("job_id", job_id)
                .execute()
            )

            return {
                "job_id": job_id,
                "event_type_distribution": stats,
                "total_events": len(events_result.data or []),
                "total_transitions": len(transitions_result.data or []),
                "most_common_event": max(stats, key=stats.get) if stats else None,
            }
        except Exception as exc:
            logger.warning("Failed to compute event statistics for job=%s: %s", job_id, exc)
            return {"error": "statistics_failed"}
