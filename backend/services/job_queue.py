"""Job Queue architecture foundation for reliable background execution.

This module is NOT a fully implemented queue worker.
It provides the architecture foundation for migrating from FastAPI BackgroundTasks
(which are in-memory and lose jobs on server restart) to a durable queue system.

Current architecture:
  - FastAPI BackgroundTasks: jobs are lost if the server restarts while running
  - In-memory asyncio.Semaphore: concurrency control is local-only

Future worker migration:
  - A dedicated worker process polls the queue
  - Redis-backed queue stores jobs
  - Workers can be scaled horizontally
  - Jobs survive server restarts

Design decisions:
  - The job table itself is the source of truth for queue state
  - Redis is used for distributed locking and event streaming
  - The queue uses a "status" column to track job lifecycle

Migration path:
  Phase 1 (now): Queue foundation + state tracking
  Phase 2 (future): Redis queue + standalone worker
  Phase 3 (future): Worker pool with auto-scaling
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from core.config import get_settings
from core.database import get_supabase
from models.job import JobStatus
from services.redis_service import RedisService

logger = logging.getLogger(__name__)

# Redis key for queue-level coordination
_QUEUE_LOCK_PREFIX = "job_queue:lock"
_QUEUE_HEARTBEAT_PREFIX = "job_queue:heartbeat"

# Job statuses that indicate "in queue" or "in progress"
_ACTIVE_STATUSES = {JobStatus.QUEUED.value, JobStatus.RUNNING.value, JobStatus.WAITING_FOR_LLM.value}


class JobQueue:
    """Architecture foundation for durable job queue.

    Current implementation:
      - Uses the database as the source of truth
      - Uses Redis for distributed coordination and heartbeat tracking
      - Supports claiming, releasing, and heartbeating jobs

    Future implementation:
      - Will use Redis lists (LPUSH/RPOP) for the queue
      - Will have dedicated worker processes that consume from the queue
    """

    @staticmethod
    def enqueue_job(job_id: str) -> bool:
        """Mark a job as ready for execution.

        This is a no-op in the current architecture (jobs are already in the DB
        as 'queued'). In the future, this will push to a Redis queue.
        """
        try:
            redis = RedisService.get_sync_client()
            if redis:
                redis.hset(f"job_queue:pending", job_id, datetime.now(timezone.utc).isoformat())
            return True
        except Exception as exc:
            logger.warning("JobQueue.enqueue_job failed (job=%s): %s", job_id, exc)
            return False

    @staticmethod
    def claim_job(job_id: str, worker_id: str) -> bool:
        """Claim a job for execution by a specific worker.

        Uses Redis SET NX to ensure only one worker processes a job.
        """
        try:
            redis = RedisService.get_sync_client()
            if redis:
                lock_key = f"{_QUEUE_LOCK_PREFIX}:{job_id}"
                acquired = redis.set(lock_key, worker_id, nx=True, ex=300)
                if acquired:
                    heartbeat_key = f"{_QUEUE_HEARTBEAT_PREFIX}:{job_id}"
                    redis.set(heartbeat_key, datetime.now(timezone.utc).isoformat(), ex=300)
                    return True
                return False
            # Fallback: no Redis, just mark in DB
            get_supabase().table("jobs").update(
                {"status": JobStatus.RUNNING.value}
            ).eq("id", job_id).execute()
            return True
        except Exception as exc:
            logger.warning("JobQueue.claim_job failed (job=%s): %s", job_id, exc)
            return False

    @staticmethod
    def heartbeat(job_id: str, worker_id: str) -> bool:
        """Update the heartbeat for a claimed job.

        Should be called periodically during execution. If the heartbeat
        expires, the job is considered orphaned and another worker can claim it.
        """
        try:
            redis = RedisService.get_sync_client()
            if redis:
                lock_key = f"{_QUEUE_LOCK_PREFIX}:{job_id}"
                current_owner = redis.get(lock_key)
                if current_owner and current_owner.decode() == worker_id:
                    heartbeat_key = f"{_QUEUE_HEARTBEAT_PREFIX}:{job_id}"
                    redis.set(heartbeat_key, datetime.now(timezone.utc).isoformat(), ex=300)
                    return True
            return False
        except Exception as exc:
            logger.warning("JobQueue.heartbeat failed (job=%s): %s", job_id, exc)
            return False

    @staticmethod
    def release_job(job_id: str, worker_id: str) -> bool:
        """Release a job lock after completion.

        Removes the Redis lock and heartbeat keys.
        """
        try:
            redis = RedisService.get_sync_client()
            if redis:
                lock_key = f"{_QUEUE_LOCK_PREFIX}:{job_id}"
                current_owner = redis.get(lock_key)
                if current_owner and current_owner.decode() == worker_id:
                    redis.delete(lock_key)
                redis.delete(f"{_QUEUE_HEARTBEAT_PREFIX}:{job_id}")
            return True
        except Exception as exc:
            logger.warning("JobQueue.release_job failed (job=%s): %s", job_id, exc)
            return False

    @staticmethod
    def is_job_claimed(job_id: str) -> bool:
        """Check if a job is currently claimed by a worker."""
        try:
            redis = RedisService.get_sync_client()
            if redis:
                return bool(redis.exists(f"{_QUEUE_LOCK_PREFIX}:{job_id}"))
            return False
        except Exception:
            return False

    @staticmethod
    def is_job_heartbeat_stale(job_id: str, max_seconds: int = 300) -> bool:
        """Check if a job's heartbeat is stale (indicating a likely crash).

        Returns True if the heartbeat is missing or older than max_seconds.
        """
        try:
            redis = RedisService.get_sync_client()
            if redis:
                heartbeat_key = f"{_QUEUE_HEARTBEAT_PREFIX}:{job_id}"
                heartbeat = redis.get(heartbeat_key)
                if not heartbeat:
                    return True
                last_beat = datetime.fromisoformat(heartbeat.decode())
                age = (datetime.now(timezone.utc) - last_beat).total_seconds()
                return age > max_seconds
            return True
        except Exception:
            return True

    @staticmethod
    def list_active_jobs() -> list[dict[str, Any]]:
        """List all jobs that are currently in active status.

        Used by the recovery system to detect unfinished jobs.
        """
        try:
            result = (
                get_supabase()
                .table("jobs")
                .select("id,user_id,workspace_id,server_id,objective,status,updated_at,created_at")
                .in_("status", list(_ACTIVE_STATUSES))
                .is_("deleted_at", "null")
                .order("updated_at", desc=False)
                .execute()
            )
            return result.data or []
        except Exception as exc:
            logger.warning("JobQueue.list_active_jobs failed: %s", exc)
            return []


# =============================================================================
# BackgroundTask Audit
# =============================================================================


def audit_background_tasks() -> dict[str, Any]:
    """Audit all BackgroundTasks usage in the system.

    Returns a diagnostic report identifying risks and migration gaps.
    """
    return {
        "audit_timestamp": datetime.now(timezone.utc).isoformat(),
        "findings": [
            {
                "location": "routers/jobs.py::submit_job",
                "risk": "HIGH",
                "description": "FastAPI BackgroundTasks run in-process. If the server restarts while a job is running, the job is lost without state.",
                "mitigation": "Job state is tracked in DB and job_events. On restart, detect_orphaned_jobs can recover jobs.",
                "migration_path": "Phase 2: Use JobQueue.enqueue_job + Redis queue. Phase 3: Dedicated worker processes.",
            },
            {
                "location": "routers/agents.py::forge_v2_run_async",
                "risk": "HIGH",
                "description": "Same as jobs.py — uses BackgroundTasks.add_task(AgentService.run_job, ...).",
                "mitigation": "Same mitigation as jobs.py.",
                "migration_path": "Phase 2: Use JobQueue.enqueue_job + Redis queue.",
            },
            {
                "location": "services/agent_service.py::run_job",
                "risk": "MEDIUM",
                "description": "Uses asyncio.Semaphore for concurrency. Semaphore is in-memory only — no distributed coordination.",
                "mitigation": "Semaphore limit is set via AGENT_MAX_CONCURRENCY. In a multi-instance deployment, this could lead to oversubscription.",
                "migration_path": "Phase 2: Use Redis-based distributed semaphore or queue per worker instance.",
            },
            {
                "location": "services/agent_service.py::run_job (executor)",
                "risk": "MEDIUM",
                "description": "SSH workspace lock is per-process. Redis key forge:lock:{workspace_id} is used but not fully coordinated.",
                "mitigation": "Redis lock is used for workspace-level concurrency. JobQueue.claim_job adds worker-level coordination.",
                "migration_path": "Phase 2: Use Redis distributed lock with proper TTL and heartbeat.",
            },
        ],
        "recommendations": [
            "Implement JobQueue.enqueue_job in all router entry points.",
            "Add a standalone worker that polls the queue (Phase 2).",
            "Use Redis for distributed concurrency control.",
            "Add health checks for worker liveness.",
        ],
    }
