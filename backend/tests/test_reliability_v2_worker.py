"""Tests for ThinkSync Reliability Sprint v2 Worker Service.

These tests verify the WorkerService, JobQueue, and recovery mechanisms
without requiring a live database, by mocking get_supabase() calls.
"""
from __future__ import annotations

import unittest
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch, AsyncMock

import asyncio


def _make_mock():
    """Create a mock supabase-like chain with proper fluent interface."""
    mock_table = MagicMock()
    mock_query = MagicMock()
    mock_table.select.return_value = mock_query
    mock_table.insert.return_value = mock_query
    mock_table.update.return_value = mock_query
    mock_table.delete.return_value = mock_query
    mock_table.upsert.return_value = mock_query
    mock_query.eq.return_value = mock_query
    mock_query.is_.return_value = mock_query
    mock_query.in_.return_value = mock_query
    mock_query.lt.return_value = mock_query
    mock_query.gt.return_value = mock_query
    mock_query.order.return_value = mock_query
    mock_query.limit.return_value = mock_query
    mock_query.maybe_single.return_value = mock_query
    mock_query.execute.return_value = _MockResult()
    return mock_table, mock_query


class _MockResult:
    """Mock supabase result."""
    def __init__(self, data=None):
        self.data = data or []


# =============================================================================
# WorkerService Tests
# =============================================================================


class WorkerServiceTests(unittest.TestCase):
    def setUp(self):
        self.mock_table, self.mock_query = _make_mock()

    def _setup_patch(self, target):
        return patch(target, return_value=MagicMock(table=MagicMock(return_value=self.mock_table)))

    def test_claim_next_job_returns_job(self):
        """Worker should claim a queued job atomically."""
        self.mock_query.execute.return_value = _MockResult([{"id": "job-1", "status": "queued"}])
        with self._setup_patch("services.worker_service.get_supabase"):
            from services.worker_service import WorkerService
            worker = WorkerService()
            job = worker._claim_next_job()
            self.assertIsNotNone(job)
            self.assertEqual(job["id"], "job-1")
            # Verify the update payload contains running status
            update_payload = self.mock_table.update.call_args[0][0]
            self.assertEqual(update_payload["status"], "running")
            self.assertEqual(update_payload["worker_id"], worker.worker_id)

    def test_claim_next_job_returns_none_when_no_jobs(self):
        """Worker should return None when no queued jobs exist."""
        self.mock_query.execute.return_value = _MockResult([])
        with self._setup_patch("services.worker_service.get_supabase"):
            from services.worker_service import WorkerService
            worker = WorkerService()
            job = worker._claim_next_job()
            self.assertIsNone(job)

    def test_detect_stale_jobs_returns_list(self):
        """Detect stale jobs with old heartbeats."""
        self.mock_query.execute.return_value = _MockResult([{"id": "job-1", "heartbeat_at": "2024-01-01T00:00:00+00:00"}])
        with self._setup_patch("services.worker_service.get_supabase"):
            from services.worker_service import WorkerService
            stale = WorkerService.detect_stale_jobs(max_seconds=1)
            self.assertEqual(len(stale), 1)
            self.assertEqual(stale[0]["id"], "job-1")

    def test_recover_stale_jobs_marks_recoverable(self):
        """Stale jobs should be marked as recoverable and queued."""
        self.mock_query.execute.return_value = _MockResult([{"id": "job-1", "heartbeat_at": "2024-01-01T00:00:00+00:00"}])
        with self._setup_patch("services.worker_service.get_supabase"):
            from services.worker_service import WorkerService
            result = WorkerService.recover_stale_jobs(max_seconds=1)
            self.assertEqual(result["stale_count"], 1)
            self.assertEqual(len(result["recovered"]), 1)
            self.assertEqual(result["recovered"][0], "job-1")

    def test_cleanup_dead_workers_marks_stale(self):
        """Dead workers should be marked stale and their jobs released."""
        self.mock_query.execute.return_value = _MockResult([{"worker_id": "worker-1", "job_id": "job-1"}])
        with self._setup_patch("services.worker_service.get_supabase"):
            from services.worker_service import WorkerService
            result = WorkerService.cleanup_dead_workers(max_seconds=1)
            self.assertEqual(result["dead_count"], 1)
            self.assertEqual(len(result["cleaned"]), 1)

    def test_mark_completed_updates_status(self):
        """Mark job as completed should update DB and clear worker."""
        self.mock_query.execute.return_value = _MockResult([])
        with self._setup_patch("services.worker_service.get_supabase"):
            from services.worker_service import WorkerService
            worker = WorkerService()
            worker._mark_completed("job-1")
            update_payload = self.mock_table.update.call_args[0][0]
            self.assertEqual(update_payload["status"], "completed")
            self.assertIsNone(update_payload["worker_id"])

    def test_mark_failed_updates_status(self):
        """Mark job as failed should update DB with error."""
        self.mock_query.execute.return_value = _MockResult([])
        with self._setup_patch("services.worker_service.get_supabase"):
            from services.worker_service import WorkerService
            worker = WorkerService()
            worker._mark_failed("job-1", error="Test error")
            update_payload = self.mock_table.update.call_args[0][0]
            self.assertEqual(update_payload["status"], "failed")
            self.assertEqual(update_payload["summary"], "Test error")

    def test_mark_abandoned_resets_to_queued(self):
        """Abandoned job should be reset to queued and marked recoverable."""
        self.mock_query.execute.return_value = _MockResult([])
        with self._setup_patch("services.worker_service.get_supabase"):
            from services.worker_service import WorkerService
            worker = WorkerService()
            worker._mark_abandoned("job-1", reason="heartbeat_timeout")
            update_payload = self.mock_table.update.call_args[0][0]
            self.assertEqual(update_payload["status"], "queued")
            self.assertTrue(update_payload["recoverable"])
            self.assertIn("Abandoned", update_payload["recovery_reason"])


# =============================================================================
# JobQueue Tests
# =============================================================================


class JobQueueTests(unittest.TestCase):
    def setUp(self):
        self.mock_table, self.mock_query = _make_mock()

    def _setup_patch(self, target):
        return patch(target, return_value=MagicMock(table=MagicMock(return_value=self.mock_table)))

    def test_enqueue_job_updates_redis(self):
        """enqueue_job should update Redis pending hash."""
        mock_redis = MagicMock()
        with patch("services.job_queue.RedisService.get_sync_client", return_value=mock_redis):
            from services.job_queue import JobQueue
            ok = JobQueue.enqueue_job("job-1")
            self.assertTrue(ok)
            mock_redis.hset.assert_called_once()

    def test_claim_job_acquired(self):
        """claim_job should acquire Redis lock."""
        mock_redis = MagicMock()
        mock_redis.set.return_value = True
        with patch("services.job_queue.RedisService.get_sync_client", return_value=mock_redis):
            from services.job_queue import JobQueue
            ok = JobQueue.claim_job("job-1", "worker-1")
            self.assertTrue(ok)

    def test_is_job_claimed_returns_true(self):
        """is_job_claimed should return True when lock exists."""
        mock_redis = MagicMock()
        mock_redis.exists.return_value = 1
        with patch("services.job_queue.RedisService.get_sync_client", return_value=mock_redis):
            from services.job_queue import JobQueue
            claimed = JobQueue.is_job_claimed("job-1")
            self.assertTrue(claimed)

    def test_is_job_heartbeat_stale_returns_true_when_missing(self):
        """is_job_heartbeat_stale should return True when heartbeat is missing."""
        mock_redis = MagicMock()
        mock_redis.get.return_value = None
        with patch("services.job_queue.RedisService.get_sync_client", return_value=mock_redis):
            from services.job_queue import JobQueue
            stale = JobQueue.is_job_heartbeat_stale("job-1")
            self.assertTrue(stale)

    def test_list_active_jobs_returns_list(self):
        """list_active_jobs should return active jobs."""
        self.mock_query.execute.return_value = _MockResult([{"id": "job-1", "status": "running"}])
        with self._setup_patch("services.job_queue.get_supabase"):
            from services.job_queue import JobQueue
            jobs = JobQueue.list_active_jobs()
            self.assertEqual(len(jobs), 1)
            self.assertEqual(jobs[0]["status"], "running")


# =============================================================================
# Router Integration Tests
# =============================================================================


class RouterIntegrationTests(unittest.TestCase):
    def test_jobs_router_no_background_tasks(self):
        """jobs.py submit_job should not call BackgroundTasks.add_task."""
        # This is a design test: verify the router file content
        import os
        router_path = os.path.join(os.path.dirname(__file__), "..", "routers", "jobs.py")
        with open(router_path) as f:
            content = f.read()
        self.assertNotIn("background_tasks.add_task", content.lower())
        self.assertNotIn("agentservice.run_job", content)
        self.assertIn("JobQueue.enqueue_job", content)

    def test_agents_router_no_background_tasks(self):
        """agents.py forge_v2_run should not call BackgroundTasks.add_task."""
        import os
        router_path = os.path.join(os.path.dirname(__file__), "..", "routers", "agents.py")
        with open(router_path) as f:
            content = f.read()
        self.assertNotIn("background_tasks.add_task", content.lower())
        self.assertNotIn("agentservice.run_job", content)
        self.assertIn("JobQueue.enqueue_job", content)


# =============================================================================
# Recovery Integration Tests
# =============================================================================


class RecoveryIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.mock_table, self.mock_query = _make_mock()

    def _setup_patch(self, target):
        return patch(target, return_value=MagicMock(table=MagicMock(return_value=self.mock_table)))

    def test_main_lifespan_includes_recovery_loop(self):
        """main.py lifespan should include recovery loop."""
        import os
        main_path = os.path.join(os.path.dirname(__file__), "..", "main.py")
        with open(main_path) as f:
            content = f.read()
        self.assertIn("recover_stale_jobs", content)
        self.assertIn("cleanup_dead_workers", content)
        self.assertIn("WorkerService", content)


if __name__ == "__main__":
    unittest.main()
