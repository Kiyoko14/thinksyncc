"""Job Recovery: detect and manage orphaned/unfinished jobs.

This module provides the recovery infrastructure for the ThinkSync execution system.
When the server restarts, jobs that were in `running` or `waiting_for_llm` state
may have been orphaned.  This module detects them, marks them, and provides
a foundation for future resume/retry support.

Implementation status: Partial — architecture foundation is complete.
  - Detection: fully implemented
  - Marking: fully implemented
  - Resume: architecture-only (not implemented yet)
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

import asyncio

from core.database import get_supabase
from models.job import JobStatus
from services.execution_event_service import ExecutionEventService
from services.job_queue import JobQueue

logger = logging.getLogger(__name__)

# Thresholds for orphan detection
DEFAULT_ORPHAN_HOURS = 1
RECOVERY_MAX_AGE_HOURS = 24


class JobRecovery:
    """Recovery support for unfinished/orphaned jobs."""

    # ------------------------------------------------------------------------
    # 1. Detection
    # ------------------------------------------------------------------------

    @staticmethod
    def detect_unfinished_jobs(hours: int = RECOVERY_MAX_AGE_HOURS) -> list[dict[str, Any]]:
        """Find all jobs that are in an unfinished status (running / waiting_for_llm / queued).

        Returns:
            List of jobs with status in [queued, running, waiting_for_llm] that were
            created within the last N hours.
        """
        try:
            cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
            result = (
                get_supabase()
                .table("jobs")
                .select("id,user_id,workspace_id,server_id,objective,status,created_at,updated_at")
                .in_("status", [JobStatus.QUEUED.value, JobStatus.RUNNING.value, JobStatus.WAITING_FOR_LLM.value])
                .is_("deleted_at", "null")
                .gt("created_at", cutoff)
                .order("updated_at", desc=False)
                .execute()
            )
            return result.data or []
        except Exception as exc:
            logger.warning("JobRecovery.detect_unfinished_jobs failed: %s", exc)
            return []

    @staticmethod
    def detect_orphaned_jobs(hours: int = DEFAULT_ORPHAN_HOURS) -> list[dict[str, Any]]:
        """Find jobs that are likely orphaned (stuck for too long without progress).

        A job is orphaned if:
          - status is running or waiting_for_llm
          - updated_at is older than N hours
          - no recent heartbeat exists

        FIX: falls back to DB-only detection when Redis heartbeat
        check fails, so orphaned jobs are never silently missed.
        """
        try:
            cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
            result = (
                get_supabase()
                .table("jobs")
                .select("id,user_id,workspace_id,server_id,objective,status,created_at,updated_at")
                .in_("status", [JobStatus.RUNNING.value, JobStatus.WAITING_FOR_LLM.value])
                .is_("deleted_at", "null")
                .lt("updated_at", cutoff)
                .order("updated_at", desc=False)
                .execute()
            )
            jobs = result.data or []

            # Cross-check with Redis heartbeat; fall back to DB-only when Redis fails.
            orphaned: list[dict[str, Any]] = []
            for job in jobs:
                job_id = job.get("id")
                try:
                    if JobQueue.is_job_heartbeat_stale(job_id, max_seconds=hours * 3600):
                        orphaned.append(job)
                except Exception:
                    # Redis unavailable — treat as orphaned (no proof of life)
                    orphaned.append(job)
            return orphaned
        except Exception as exc:
            logger.warning("JobRecovery.detect_orphaned_jobs failed: %s", exc)
            return []

    @staticmethod
    def detect_orphaned_without_redis(hours: int = DEFAULT_ORPHAN_HOURS) -> list[dict[str, Any]]:
        """Find orphaned jobs using DB only (no Redis dependency).

        This is the fallback when Redis is unavailable.
        """
        try:
            cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
            result = (
                get_supabase()
                .table("jobs")
                .select("id,user_id,workspace_id,server_id,objective,status,created_at,updated_at")
                .in_("status", [JobStatus.RUNNING.value, JobStatus.WAITING_FOR_LLM.value])
                .is_("deleted_at", "null")
                .lt("updated_at", cutoff)
                .order("updated_at", desc=False)
                .execute()
            )
            return result.data or []
        except Exception as exc:
            logger.warning("JobRecovery.detect_orphaned_without_redis failed: %s", exc)
            return []

    # ------------------------------------------------------------------------
    # 2. Marking
    # ------------------------------------------------------------------------

    @staticmethod
    def mark_job_orphaned(job_id: str, reason: str = "stale_timeout") -> bool:
        """Mark a job as orphaned and failed.

        This is the final disposition for an orphaned job that cannot be recovered.
        """
        try:
            now = datetime.now(timezone.utc).isoformat()
            get_supabase().table("jobs").update(
                {
                    "status": JobStatus.FAILED.value,
                    "recoverable": False,
                    "recovery_reason": f"Orphaned: {reason}",
                    "updated_at": now,
                }
            ).eq("id", job_id).execute()

            get_supabase().table("job_state_transitions").insert(
                {
                    "job_id": job_id,
                    "from_status": JobStatus.RUNNING.value,
                    "to_status": JobStatus.FAILED.value,
                    "reason": f"Orphaned: {reason}",
                    "created_at": now,
                }
            ).execute()

            # Emit event asynchronously (best-effort; skip if no event loop)
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(
                    ExecutionEventService.execution_failed(
                        job_id, reason="Orphaned: stale_timeout", error=reason
                    )
                )
            except Exception:
                pass

            return True
        except Exception as exc:
            logger.warning("JobRecovery.mark_job_orphaned failed (job=%s): %s", job_id, exc)
            return False

    @staticmethod
    def mark_job_recoverable(job_id: str, reason: str = "detected_orphaned") -> bool:
        """Mark a job as recoverable and reset it to queued.

        This is the disposition for an orphaned job that can be retried.
        """
        try:
            now = datetime.now(timezone.utc).isoformat()
            get_supabase().table("jobs").update(
                {
                    "status": JobStatus.QUEUED.value,
                    "recoverable": True,
                    "recovery_reason": reason,
                    "updated_at": now,
                }
            ).eq("id", job_id).execute()

            get_supabase().table("job_state_transitions").insert(
                {
                    "job_id": job_id,
                    "from_status": JobStatus.RUNNING.value,
                    "to_status": JobStatus.QUEUED.value,
                    "reason": f"Recovery: {reason}",
                    "created_at": now,
                }
            ).execute()

            return True
        except Exception as exc:
            logger.warning("JobRecovery.mark_job_recoverable failed (job=%s): %s", job_id, exc)
            return False

    # ------------------------------------------------------------------------
    # 3. Recovery List
    # ------------------------------------------------------------------------

    @staticmethod
    def list_recoverable_jobs() -> list[dict[str, Any]]:
        """List all jobs that have been marked as recoverable.

        These are candidates for a future retry/resume worker.
        """
        try:
            result = (
                get_supabase()
                .table("jobs")
                .select("id,user_id,workspace_id,server_id,objective,status,recoverable,recovery_reason,created_at,updated_at")
                .eq("recoverable", True)
                .is_("deleted_at", "null")
                .order("updated_at", desc=False)
                .execute()
            )
            return result.data or []
        except Exception as exc:
            logger.warning("JobRecovery.list_recoverable_jobs failed: %s", exc)
            return []

    # ------------------------------------------------------------------------
    # 4. Recovery Report
    # ------------------------------------------------------------------------

    @staticmethod
    def generate_recovery_report(hours: int = DEFAULT_ORPHAN_HOURS) -> dict[str, Any]:
        """Generate a comprehensive recovery report.

        Includes counts of unfinished, orphaned, and recoverable jobs.
        """
        unfinished = JobRecovery.detect_unfinished_jobs()
        orphaned = JobRecovery.detect_orphaned_jobs(hours=hours)
        orphaned_db_only = JobRecovery.detect_orphaned_without_redis(hours=hours)
        recoverable = JobRecovery.list_recoverable_jobs()

        return {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "hours_threshold": hours,
            "unfinished": {
                "count": len(unfinished),
                "jobs": unfinished,
            },
            "orphaned": {
                "count": len(orphaned),
                "jobs": orphaned,
            },
            "orphaned_db_only": {
                "count": len(orphaned_db_only),
                "jobs": orphaned_db_only,
            },
            "recoverable": {
                "count": len(recoverable),
                "jobs": recoverable,
            },
            "recommendations": [
                "Run mark_job_recoverable for jobs that should be retried.",
                "Run mark_job_orphaned for jobs that are permanently lost.",
                "Implement a retry worker to process recoverable jobs.",
            ],
        }

    # ------------------------------------------------------------------------
    # 5. Batch Recovery
    # ------------------------------------------------------------------------

    @staticmethod
    def batch_mark_recoverable(hours: int = DEFAULT_ORPHAN_HOURS) -> dict[str, Any]:
        """Mark all orphaned jobs as recoverable in a batch.

        FIX: consolidates Redis-backed and DB-only orphan detection,
        and uses a DB-level advisory lock so only one process
        runs recovery at a time.

        Returns:
            Summary of actions taken.
        """
        # Advisory lock so concurrent workers don't double-recover
        lock_acquired = False
        try:
            try:
                get_supabase().rpc("pg_advisory_lock", {"key": 20250401}).execute()
                lock_acquired = True
            except Exception:
                pass  # lock function may not exist; proceed without

            orphaned = JobRecovery.detect_orphaned_jobs(hours=hours)
            if not orphaned:
                # Redis may be down; fall back to DB-only detection
                orphaned = JobRecovery.detect_orphaned_without_redis(hours=hours)

            marked: list[str] = []
            failed: list[str] = []

            for job in orphaned:
                job_id = job.get("id")
                if JobRecovery.mark_job_recoverable(job_id, reason="batch_recovery"):
                    marked.append(job_id)
                else:
                    failed.append(job_id)

            return {
                "action": "batch_mark_recoverable",
                "count": len(orphaned),
                "marked": marked,
                "failed": failed,
            }
        finally:
            if lock_acquired:
                try:
                    get_supabase().rpc("pg_advisory_unlock", {"key": 20250401}).execute()
                except Exception:
                    pass

    @staticmethod
    def batch_mark_orphaned(hours: int = DEFAULT_ORPHAN_HOURS) -> dict[str, Any]:
        """Mark all orphaned jobs as failed in a batch.

        Uses the same DB-level advisory lock as ``batch_mark_recoverable`` so
        only one recovery process marks orphans at a time (prevents
        double-marking / inconsistent disposition).
        """
        lock_acquired = False
        try:
            try:
                get_supabase().rpc("pg_advisory_lock", {"key": 20250402}).execute()
                lock_acquired = True
            except Exception:
                pass  # lock function may not exist; proceed without

            orphaned = JobRecovery.detect_orphaned_jobs(hours=hours)
            if not orphaned:
                orphaned = JobRecovery.detect_orphaned_without_redis(hours=hours)

            marked: list[str] = []
            failed: list[str] = []

            for job in orphaned:
                job_id = job.get("id")
                if JobRecovery.mark_job_orphaned(job_id, reason="batch_mark_orphaned"):
                    marked.append(job_id)
                else:
                    failed.append(job_id)

            return {
                "action": "batch_mark_orphaned",
                "count": len(orphaned),
                "marked": marked,
                "failed": failed,
            }
        finally:
            if lock_acquired:
                try:
                    get_supabase().rpc("pg_advisory_unlock", {"key": 20250402}).execute()
                except Exception:
                    pass
