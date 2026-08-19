"""Worker Service: durable queue execution with heartbeat and recovery.

Architecture:
- PostgreSQL jobs table is the durable queue (single source of truth)
- Redis used for distributed lock/heartbeat (fast, but not durable)
- Workers claim jobs atomically via DB
- Workers heartbeat into DB + Redis
- Dead worker detection via heartbeat timeout
- Recovery integration with existing JobRecovery framework

Key design decisions:
1. DB is the source of truth for queue state. Redis is optimization only.
2. Job claiming is atomic via UPDATE ... WHERE status = 'queued'.
3. Worker crashes are detected via heartbeat_at staleness.
4. Every worker action emits events to job_events + job_state_transitions.
5. Backward compatible: existing APIs, frontend, jobs table unchanged.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from core.database import get_supabase
from models.job import JobCreate, JobStatus
from services import logger as obs
from services.execution_event_service import ExecutionEventService
from services.execution_repository import save_execution_detail
from services.redis_service import RedisService

logger = logging.getLogger(__name__)

# Tunable constants
WORKER_ID = os.environ.get("WORKER_ID", f"worker-{uuid4().hex[:8]}")
CLAIM_TIMEOUT_SECONDS = 60  # Max time to wait for a job claim
HEARTBEAT_INTERVAL_SECONDS = 15
HEARTBEAT_TIMEOUT_SECONDS = 60  # If no heartbeat in 60s, worker is dead
JOB_STALE_SECONDS = 120  # If job heartbeat > 120s, job is abandoned
POLL_INTERVAL_SECONDS = 2  # How often to poll the queue
MAX_CONCURRENT_JOBS = 1  # One job per worker (simple, reliable)

# Redis keys
_REDIS_LOCK_PREFIX = "job:lock"
_REDIS_HEARTBEAT_PREFIX = "worker:heartbeat"


# ---------------------------------------------------------------------------
# Worker service
# ---------------------------------------------------------------------------

class WorkerService:
    """Durable worker that claims jobs from the DB queue and executes them.

    Each worker has a unique worker_id. It polls for queued jobs, claims one
    atomically, heartbeats during execution, and marks completion/failure.

    Crash safety: if a worker crashes, the heartbeat_at becomes stale.
    Another worker or the recovery system will detect this and mark the job
    for retry or failure.
    """

    _instance: WorkerService | None = None
    _shutdown_event: asyncio.Event | None = None

    def __init__(self) -> None:
        self.worker_id = WORKER_ID
        self._running = False
        self._current_job_id: str | None = None
        self._shutdown_event = asyncio.Event()
        WorkerService._instance = self

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @classmethod
    def get_instance(cls) -> WorkerService:
        if cls._instance is None:
            cls._instance = WorkerService()
        return cls._instance

    @classmethod
    def is_worker_running(cls) -> bool:
        inst = cls._instance
        return inst is not None and inst._running

    async def run(self) -> None:
        """Main worker loop. Polls queue, claims, executes, repeats."""
        self._running = True
        logger.info("Worker started | worker_id=%s", self.worker_id)
        obs.emit(
            level="INFO",
            layer="worker",
            message="worker_started",
            meta={"worker_id": self.worker_id},
        )

        # Register worker heartbeat
        self._register_worker()

        try:
            while not self._shutdown_event.is_set():
                try:
                    self._heartbeat_worker()
                    job = self._claim_next_job()
                    if job:
                        job_id = job["id"]
                        self._current_job_id = job_id
                        await self._execute_job(job)
                        self._current_job_id = None
                    else:
                        # No job available; sleep before polling again
                        await asyncio.wait_for(
                            self._shutdown_event.wait(), timeout=POLL_INTERVAL_SECONDS
                        )
                except asyncio.TimeoutError:
                    # Normal timeout from wait_for — continue loop
                    pass
                except asyncio.CancelledError:
                    break
                except Exception as exc:
                    logger.exception("Worker loop error | worker=%s", self.worker_id)
                    obs.emit(
                        level="ERROR",
                        layer="worker",
                        message="worker_loop_error",
                        meta={"worker_id": self.worker_id, "error": str(exc)},
                    )
                    try:
                        await asyncio.wait_for(
                            self._shutdown_event.wait(), timeout=POLL_INTERVAL_SECONDS
                        )
                    except asyncio.TimeoutError:
                        pass
        finally:
            self._running = False
            self._mark_worker_shutdown()
            logger.info("Worker stopped | worker_id=%s", self.worker_id)

    def stop(self) -> None:
        """Signal the worker to stop gracefully."""
        self._shutdown_event.set()
        if self._current_job_id:
            # Release current job so another worker can pick it up
            self._release_job(self._current_job_id, reason="worker_shutdown")

    # ------------------------------------------------------------------
    # Job claiming (atomic)
    # ------------------------------------------------------------------

    def _claim_next_job(self) -> dict[str, Any] | None:
        """Atomically claim the next queued job.

        Uses DB UPDATE with status filter to prevent race conditions.
        Returns the job dict or None if no jobs available.

        FIX: retries DB claim on transient errors; limits per-cycle
        claim attempts to avoid busy-looping on DB failures.
        """
        for attempt in range(3):
            try:
                now = datetime.now(timezone.utc).isoformat()
                result = (
                    get_supabase()
                    .table("jobs")
                    .update(
                        {
                            "status": JobStatus.RUNNING.value,
                            "worker_id": self.worker_id,
                            "claimed_at": now,
                            "heartbeat_at": now,
                            "updated_at": now,
                        }
                    )
                    .eq("status", JobStatus.QUEUED.value)
                    .is_("deleted_at", "null")
                    .execute()
                )

                if not result.data:
                    return None

                job = result.data[0]
                job_id = job["id"]

                # Emit event
                self._emit_worker_event(job_id, "worker_claimed", {"worker_id": self.worker_id})
                try:
                    loop = asyncio.get_running_loop()
                    loop.create_task(
                        ExecutionEventService.state_transition(
                            job_id,
                            JobStatus.QUEUED.value,
                            JobStatus.RUNNING.value,
                            reason=f"Claimed by worker {self.worker_id}",
                        )
                    )
                except Exception:
                    pass

                # Redis lock (optimization, not required)
                try:
                    redis = RedisService.get_sync_client()
                    if redis:
                        redis.set(f"{_REDIS_LOCK_PREFIX}:{job_id}", self.worker_id, ex=300)
                except Exception:
                    pass

                logger.info("Worker claimed job | worker=%s | job=%s", self.worker_id, job_id)
                return job
            except Exception as exc:
                if attempt < 2:
                    logger.warning("Worker claim attempt %s failed | worker=%s: %s", attempt + 1, self.worker_id, exc)
                    try:
                        import time as _time
                        _time.sleep(1)
                    except Exception:
                        pass
                    continue
                logger.warning("Worker claim failed after retries | worker=%s: %s", self.worker_id, exc)
                return None

    # ------------------------------------------------------------------
    # Job execution
    # ------------------------------------------------------------------

    async def _execute_job(self, job: dict[str, Any]) -> None:
        """Execute a claimed job.

        This wraps the existing AgentService.run_job logic with heartbeat
        and safe failure handling.
        """
        job_id = job["id"]
        user_id = job["user_id"]
        workspace_id = job.get("workspace_id")
        server_id = job["server_id"]
        objective = job["objective"]
        max_steps = job.get("max_steps", 8)
        allow_write = job.get("allow_write", True)
        dry_run = job.get("dry_run", False)
        step_timeout = job.get("step_timeout_seconds")

        # Start heartbeat task
        heartbeat_task = asyncio.create_task(self._heartbeat_loop(job_id))

        try:
            # Build payload
            payload = JobCreate(
                workspace_id=workspace_id,
                server_id=server_id,
                objective=objective,
                max_steps=max_steps,
                allow_write=allow_write,
                dry_run=dry_run,
                step_timeout_seconds=step_timeout,
            )

            # Run the job via AgentService, bypassing the semaphore
            # (the worker manages its own concurrency via MAX_CONCURRENT_JOBS=1).
            from services.agent_service import AgentService

            await AgentService.run_job(job_id, payload, user_id, trace_id=obs.new_trace_id(), bypass_semaphore=True)

            # Mark completed
            self._mark_completed(job_id)
            self._emit_worker_event(job_id, "worker_completed", {"worker_id": self.worker_id})

        except asyncio.CancelledError:
            # Worker was stopped during execution
            self._mark_abandoned(job_id, reason="worker_cancelled")
            self._emit_worker_event(job_id, "worker_abandoned", {"worker_id": self.worker_id, "reason": "worker_cancelled"})
            raise
        except Exception as exc:
            # Mark failed
            self._mark_failed(job_id, error=str(exc))
            self._emit_worker_event(job_id, "worker_failed", {"worker_id": self.worker_id, "error": str(exc)})
            save_execution_detail(job_id, "error", {"error": str(exc), "worker_id": self.worker_id}, step_number=None)
        finally:
            heartbeat_task.cancel()
            try:
                await heartbeat_task
            except asyncio.CancelledError:
                pass
            # Release Redis lock
            try:
                redis = RedisService.get_sync_client()
                if redis:
                    redis.delete(f"{_REDIS_LOCK_PREFIX}:{job_id}")
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Heartbeat
    # ------------------------------------------------------------------

    async def _heartbeat_loop(self, job_id: str) -> None:
        """Background task that heartbeats while a job is running."""
        while True:
            try:
                await asyncio.sleep(HEARTBEAT_INTERVAL_SECONDS)
                self._heartbeat_job(job_id)
                self._heartbeat_worker()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.warning("Heartbeat error | worker=%s | job=%s: %s", self.worker_id, job_id, exc)

    def _heartbeat_job(self, job_id: str) -> None:
        """Update heartbeat timestamp for a running job."""
        now = datetime.now(timezone.utc).isoformat()
        try:
            get_supabase().table("jobs").update(
                {"heartbeat_at": now, "updated_at": now}
            ).eq("id", job_id).execute()
        except Exception as exc:
            logger.warning("Job heartbeat failed | job=%s: %s", job_id, exc)

    def _heartbeat_worker(self) -> None:
        """Update worker heartbeat in worker_heartbeats table."""
        now = datetime.now(timezone.utc).isoformat()
        try:
            get_supabase().table("worker_heartbeats").upsert(
                {
                    "worker_id": self.worker_id,
                    "job_id": self._current_job_id,
                    "last_heartbeat": now,
                    "status": "active",
                    "updated_at": now,
                },
                on_conflict="worker_id",
            ).execute()
        except Exception as exc:
            logger.warning("Worker heartbeat failed | worker=%s: %s", self.worker_id, exc)

        # Also update Redis
        try:
            redis = RedisService.get_sync_client()
            if redis:
                redis.set(f"{_REDIS_HEARTBEAT_PREFIX}:{self.worker_id}", now, ex=HEARTBEAT_TIMEOUT_SECONDS)
        except Exception:
            pass

    def _register_worker(self) -> None:
        """Register a new worker in the heartbeat table."""
        now = datetime.now(timezone.utc).isoformat()
        try:
            get_supabase().table("worker_heartbeats").upsert(
                {
                    "worker_id": self.worker_id,
                    "job_id": None,
                    "last_heartbeat": now,
                    "started_at": now,
                    "status": "active",
                    "updated_at": now,
                },
                on_conflict="worker_id",
            ).execute()
        except Exception as exc:
            logger.warning("Worker registration failed | worker=%s: %s", self.worker_id, exc)

    def _mark_worker_shutdown(self) -> None:
        """Mark worker as shutdown in the heartbeat table."""
        now = datetime.now(timezone.utc).isoformat()
        try:
            get_supabase().table("worker_heartbeats").update(
                {"status": "shutdown", "updated_at": now, "job_id": None}
            ).eq("worker_id", self.worker_id).execute()
        except Exception as exc:
            logger.warning("Worker shutdown mark failed | worker=%s: %s", self.worker_id, exc)

    # ------------------------------------------------------------------
    # Job state management
    # ------------------------------------------------------------------

    def _mark_completed(self, job_id: str) -> None:
        """Mark a job as completed.

        Guarded (Sprint 3C.C): if the job is in a waiting/paused/cancelled
        state (event-driven wait), do NOT clobber it.  A suspended job's
        status is owned by the Event Wait Engine until a resume event arrives.
        """
        # Read the current status first so we never overwrite a suspended job.
        try:
            row = (
                get_supabase()
                .table("jobs")
                .select("status")
                .eq("id", job_id)
                .limit(1)
                .execute()
            )
            current = (row.data[0].get("status") if row.data else None) if row else None
        except Exception:
            current = None
        if current in {
            JobStatus.WAITING_FOR_USER.value,
            JobStatus.PAUSED.value,
            JobStatus.CANCELLED.value,
            JobStatus.RESUMED.value,
        }:
            logger.info(
                "[worker] skip _mark_completed — job %s is in wait state %s",
                job_id,
                current,
            )
            return
        now = datetime.now(timezone.utc).isoformat()
        try:
            get_supabase().table("jobs").update(
                {
                    "status": JobStatus.COMPLETED.value,
                    "completed_at": now,
                    "worker_id": None,
                    "heartbeat_at": None,
                    "updated_at": now,
                }
            ).eq("id", job_id).execute()

            try:
                loop = asyncio.get_running_loop()
                loop.create_task(
                    ExecutionEventService.state_transition(
                        job_id,
                        JobStatus.RUNNING.value,
                        JobStatus.COMPLETED.value,
                        reason="Worker completed",
                    )
                )
            except Exception:
                pass
        except Exception as exc:
            logger.warning("Mark completed failed | job=%s: %s", job_id, exc)

    def _mark_failed(self, job_id: str, error: str | None = None) -> None:
        """Mark a job as failed."""
        now = datetime.now(timezone.utc).isoformat()
        try:
            get_supabase().table("jobs").update(
                {
                    "status": JobStatus.FAILED.value,
                    "worker_id": None,
                    "heartbeat_at": None,
                    "updated_at": now,
                    "summary": error or "Worker execution failed",
                }
            ).eq("id", job_id).execute()

            try:
                loop = asyncio.get_running_loop()
                loop.create_task(
                    ExecutionEventService.state_transition(
                        job_id,
                        JobStatus.RUNNING.value,
                        JobStatus.FAILED.value,
                        reason=f"Worker failed: {error}",
                    )
                )
            except Exception:
                pass
        except Exception as exc:
            logger.warning("Mark failed failed | job=%s: %s", job_id, exc)

    def _mark_abandoned(self, job_id: str, reason: str = "heartbeat_timeout") -> None:
        """Mark a job as abandoned (recoverable)."""
        now = datetime.now(timezone.utc).isoformat()
        try:
            get_supabase().table("jobs").update(
                {
                    "status": JobStatus.QUEUED.value,
                    "recoverable": True,
                    "recovery_reason": f"Abandoned: {reason}",
                    "worker_id": None,
                    "heartbeat_at": None,
                    "updated_at": now,
                }
            ).eq("id", job_id).execute()

            try:
                loop = asyncio.get_running_loop()
                loop.create_task(
                    ExecutionEventService.state_transition(
                        job_id,
                        JobStatus.RUNNING.value,
                        JobStatus.QUEUED.value,
                        reason=f"Abandoned: {reason}",
                    )
                )
            except Exception:
                pass

            self._emit_worker_event(job_id, "worker_abandoned", {"reason": reason})
        except Exception as exc:
            logger.warning("Mark abandoned failed | job=%s: %s", job_id, exc)

    def _release_job(self, job_id: str, reason: str = "worker_release") -> None:
        """Release a job lock (reset to queued)."""
        now = datetime.now(timezone.utc).isoformat()
        try:
            get_supabase().table("jobs").update(
                {
                    "status": JobStatus.QUEUED.value,
                    "worker_id": None,
                    "claimed_at": None,
                    "heartbeat_at": None,
                    "updated_at": now,
                }
            ).eq("id", job_id).execute()

            self._emit_worker_event(job_id, "worker_released", {"reason": reason})
        except Exception as exc:
            logger.warning("Release job failed | job=%s: %s", job_id, exc)

    # ------------------------------------------------------------------
    # Event emission
    # ------------------------------------------------------------------

    def _emit_worker_event(self, job_id: str, event_type: str, payload: dict[str, Any]) -> None:
        """Emit worker action event to job_events."""
        try:
            # Use sync event emission for worker (no async context needed)
            loop = asyncio.get_running_loop()
            loop.create_task(
                ExecutionEventService.emit(job_id, event_type, payload, trace_id=payload.get("worker_id"))
            )
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Recovery helpers
    # ------------------------------------------------------------------

    @staticmethod
    def detect_stale_jobs(max_seconds: int = JOB_STALE_SECONDS) -> list[dict[str, Any]]:
        """Find jobs that have stale heartbeats (likely abandoned)."""
        cutoff = (datetime.now(timezone.utc) - timedelta(seconds=max_seconds)).isoformat()
        try:
            result = (
                get_supabase()
                .table("jobs")
                .select("id,user_id,worker_id,heartbeat_at,status,created_at")
                .in_("status", [JobStatus.RUNNING.value, JobStatus.WAITING_FOR_LLM.value, "claimed"])
                .is_("deleted_at", "null")
                .lt("heartbeat_at", cutoff)
                .execute()
            )
            return result.data or []
        except Exception as exc:
            logger.warning("Detect stale jobs failed: %s", exc)
            return []

    @staticmethod
    def recover_stale_jobs(max_seconds: int = JOB_STALE_SECONDS) -> dict[str, Any]:
        """Mark stale jobs as abandoned/queued for recovery.

        Reuses ``WorkerService.detect_stale_jobs`` (which reads through the
        same ``get_supabase`` accessor) and re-queues each stale job as
        recoverable. This keeps the recovery path through a single query
        accessor so it stays testable and consistent with the worker's own
        stale-detection logic.
        """
        stale = WorkerService.detect_stale_jobs(max_seconds=max_seconds)
        recovered: list[str] = []
        failed: list[str] = []
        now = datetime.now(timezone.utc).isoformat()
        for job in stale:
            job_id = job.get("id")
            if not job_id:
                continue
            try:
                get_supabase().table("jobs").update(
                    {
                        "status": JobStatus.QUEUED.value,
                        "recoverable": True,
                        "recovery_reason": "Abandoned: heartbeat_timeout",
                        "worker_id": None,
                        "heartbeat_at": None,
                        "updated_at": now,
                    }
                ).eq("id", job_id).execute()
                recovered.append(job_id)
            except Exception as exc:
                logger.warning("Recover stale job failed | job=%s: %s", job_id, exc)
                failed.append(job_id)

        return {
            "stale_count": len(stale),
            "recovered": recovered,
            "failed": failed,
        }

    @staticmethod
    def detect_dead_workers(max_seconds: int = HEARTBEAT_TIMEOUT_SECONDS) -> list[dict[str, Any]]:
        """Find workers that have not heartbeated recently."""
        cutoff = (datetime.now(timezone.utc) - timedelta(seconds=max_seconds)).isoformat()
        try:
            result = (
                get_supabase()
                .table("worker_heartbeats")
                .select("worker_id,job_id,last_heartbeat,status")
                .eq("status", "active")
                .lt("last_heartbeat", cutoff)
                .execute()
            )
            return result.data or []
        except Exception as exc:
            logger.warning("Detect dead workers failed: %s", exc)
            return []

    @staticmethod
    def cleanup_dead_workers(max_seconds: int = HEARTBEAT_TIMEOUT_SECONDS) -> dict[str, Any]:
        """Mark dead workers as stale and release their jobs."""
        dead = WorkerService.detect_dead_workers(max_seconds)
        cleaned: list[str] = []
        failed: list[str] = []

        for worker in dead:
            worker_id = worker["worker_id"]
            job_id = worker.get("job_id")
            try:
                now = datetime.now(timezone.utc).isoformat()
                # Mark worker stale
                get_supabase().table("worker_heartbeats").update(
                    {"status": "stale", "updated_at": now, "job_id": None}
                ).eq("worker_id", worker_id).execute()

                # Release job if any
                if job_id:
                    get_supabase().table("jobs").update(
                        {
                            "status": JobStatus.QUEUED.value,
                            "recoverable": True,
                            "recovery_reason": f"Worker {worker_id} died",
                            "worker_id": None,
                            "heartbeat_at": None,
                            "updated_at": now,
                        }
                    ).eq("id", job_id).execute()

                cleaned.append(worker_id)
            except Exception as exc:
                logger.warning("Cleanup dead worker failed | worker=%s: %s", worker_id, exc)
                failed.append(worker_id)

        return {
            "dead_count": len(dead),
            "cleaned": cleaned,
            "failed": failed,
        }


# ---------------------------------------------------------------------------
# Standalone worker entrypoint
# ---------------------------------------------------------------------------

async def run_worker() -> None:
    """Run the worker as a standalone process.

    Usage:
        python -m services.worker_service
    """
    worker = WorkerService.get_instance()

    # Handle signals for graceful shutdown
    def _signal_handler(signum: int, frame: Any) -> None:
        logger.info("Worker received signal %s, shutting down...", signum)
        worker.stop()

    signal.signal(signal.SIGTERM, _signal_handler)
    signal.signal(signal.SIGINT, _signal_handler)

    # Run recovery before starting
    logger.info("Running startup recovery...")
    recovery = WorkerService.recover_stale_jobs()
    cleanup = WorkerService.cleanup_dead_workers()
    logger.info("Startup recovery: %s", recovery)
    logger.info("Dead worker cleanup: %s", cleanup)

    await worker.run()


if __name__ == "__main__":
    asyncio.run(run_worker())
