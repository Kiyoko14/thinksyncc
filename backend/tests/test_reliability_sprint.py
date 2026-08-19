"""Tests for ThinkSync Reliability Sprint v1 services.

These tests verify the new services without requiring a live database,
by mocking the core.database.get_supabase() calls.
"""
from __future__ import annotations

import unittest
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from models.agent import AgentDecision, DecisionAction, StepResult, ToolName


def _make_mock():
    """Create a mock supabase-like chain with proper fluent interface."""
    mock_table = MagicMock()
    mock_query = MagicMock()
    mock_table.select.return_value = mock_query
    mock_table.insert.return_value = mock_query
    mock_table.update.return_value = mock_query
    mock_table.delete.return_value = mock_query
    mock_query.eq.return_value = mock_query
    mock_query.is_.return_value = mock_query
    mock_query.in_.return_value = mock_query
    mock_query.lt.return_value = mock_query
    mock_query.gt.return_value = mock_query
    mock_query.order.return_value = mock_query
    mock_query.maybe_single.return_value = mock_query
    mock_query.execute.return_value = _MockResult()
    return mock_table, mock_query


class _MockResult:
    """Mock supabase result."""
    def __init__(self, data=None):
        self.data = data or []


# =============================================================================
# ExecutionRepository Tests
# =============================================================================


class ExecutionRepositoryTests(unittest.TestCase):
    def setUp(self):
        self.mock_table, self.mock_query = _make_mock()

    def _setup_patch(self, target):
        return patch(target, return_value=MagicMock(table=MagicMock(return_value=self.mock_table)))

    def test_save_step_persists_to_db(self):
        self.mock_query.execute.return_value = _MockResult([])
        with self._setup_patch("services.execution_repository.get_supabase"):
            from services.execution_repository import save_step
            result = StepResult(
                step=1,
                tool=ToolName.RUN_COMMAND,
                args={"command": "echo hello"},
                stdout="hello",
                stderr="",
                exit_code=0,
                duration_ms=100,
                executed_at=datetime.now(timezone.utc),
                success=True,
            )
            ok = save_step("job-1", result)
            self.assertTrue(ok)

    def test_save_step_returns_false_on_error(self):
        self.mock_query.execute.side_effect = Exception("DB error")
        with self._setup_patch("services.execution_repository.get_supabase"):
            from services.execution_repository import save_step
            result = StepResult(
                step=1,
                tool=ToolName.RUN_COMMAND,
                args={},
                stdout="",
                stderr="",
                exit_code=0,
                duration_ms=0,
                executed_at=datetime.now(timezone.utc),
                success=True,
            )
            ok = save_step("job-1", result)
            self.assertFalse(ok)

    def test_get_steps_returns_list(self):
        self.mock_query.execute.return_value = _MockResult([{"step_number": 1, "tool": "run_command"}])
        with self._setup_patch("services.execution_repository.get_supabase"):
            from services.execution_repository import get_steps
            steps = get_steps("job-1")
            self.assertEqual(len(steps), 1)
            self.assertEqual(steps[0]["step_number"], 1)

    def test_get_steps_returns_empty_on_error(self):
        self.mock_query.execute.side_effect = Exception("DB error")
        with self._setup_patch("services.execution_repository.get_supabase"):
            from services.execution_repository import get_steps
            steps = get_steps("job-1")
            self.assertEqual(steps, [])

    def test_save_decision_persists(self):
        self.mock_query.execute.return_value = _MockResult([])
        with self._setup_patch("services.execution_repository.get_supabase"):
            from services.execution_repository import save_decision
            decision = AgentDecision(
                action=DecisionAction.CONTINUE,
                reason="Looks good",
                summary_so_far="",
            )
            ok = save_decision("job-1", decision, step_number=2)
            self.assertTrue(ok)

    def test_save_retry_persists(self):
        self.mock_query.execute.return_value = _MockResult([])
        with self._setup_patch("services.execution_repository.get_supabase"):
            from services.execution_repository import save_retry
            ok = save_retry("job-1", step_number=3, attempt=2, command="npm install", command_type="WRITE", reason="timeout")
            self.assertTrue(ok)

    def test_save_execution_detail_persists(self):
        self.mock_query.execute.return_value = _MockResult([])
        with self._setup_patch("services.execution_repository.get_supabase"):
            from services.execution_repository import save_execution_detail
            ok = save_execution_detail("job-1", "error", {"message": "failed"}, step_number=1)
            self.assertTrue(ok)

    def test_reconstruct_job_execution_returns_all_keys(self):
        self.mock_query.execute.return_value = _MockResult([])
        with self._setup_patch("services.execution_repository.get_supabase"):
            from services.execution_repository import reconstruct_job_execution
            recon = reconstruct_job_execution("job-1")
            self.assertEqual(recon["job_id"], "job-1")
            self.assertIn("steps", recon)
            self.assertIn("decisions", recon)
            self.assertIn("retries", recon)
            self.assertIn("errors", recon)
            self.assertIn("metadata", recon)
            self.assertIn("analysis", recon)
            self.assertIn("contracts", recon)


# =============================================================================
# ExecutionEventService Tests
# =============================================================================


class ExecutionEventServiceTests(unittest.TestCase):
    def setUp(self):
        self.mock_table, self.mock_query = _make_mock()

    def _setup_patch(self, target):
        return patch(target, return_value=MagicMock(table=MagicMock(return_value=self.mock_table)))

    def test_emit_persists_event(self):
        self.mock_query.execute.return_value = _MockResult([])
        with self._setup_patch("services.execution_event_service.get_supabase"):
            from services.execution_event_service import ExecutionEventService
            import asyncio
            asyncio.run(
                ExecutionEventService.emit(
                    "job-1", "test_event", {"foo": "bar"}, workspace_id="ws-1", trace_id="t-1"
                )
            )
            call_args = self.mock_table.insert.call_args[0][0]
            self.assertEqual(call_args["job_id"], "job-1")
            self.assertEqual(call_args["event_type"], "test_event")
            self.assertEqual(call_args["workspace_id"], "ws-1")
            self.assertEqual(call_args["trace_id"], "t-1")

    def test_state_transition_persists_to_both_tables(self):
        self.mock_query.execute.return_value = _MockResult([])
        with self._setup_patch("services.execution_event_service.get_supabase"):
            from services.execution_event_service import ExecutionEventService
            import asyncio
            asyncio.run(
                ExecutionEventService.state_transition(
                    "job-1", "queued", "running", trace_id="t-1", reason="claimed"
                )
            )
            # state_transition inserts into job_state_transitions then calls emit which inserts into job_events
            self.assertEqual(self.mock_table.insert.call_count, 2)

    def test_job_created_event(self):
        self.mock_query.execute.return_value = _MockResult([])
        with self._setup_patch("services.execution_event_service.get_supabase"):
            from services.execution_event_service import ExecutionEventService
            import asyncio
            asyncio.run(
                ExecutionEventService.job_created("job-1", workspace_id="ws-1", trace_id="t-1", objective="deploy")
            )
            call_args = self.mock_table.insert.call_args[0][0]
            self.assertEqual(call_args["event_type"], "job_created")

    def test_execution_started_event(self):
        self.mock_query.execute.return_value = _MockResult([])
        with self._setup_patch("services.execution_event_service.get_supabase"):
            from services.execution_event_service import ExecutionEventService
            import asyncio
            asyncio.run(
                ExecutionEventService.execution_started("job-1", workspace_id="ws-1", trace_id="t-1", task_mode="complex")
            )
            call_args = self.mock_table.insert.call_args[0][0]
            self.assertEqual(call_args["event_type"], "execution_started")
            self.assertEqual(call_args["payload"]["task_mode"], "complex")

    def test_execution_completed_event(self):
        self.mock_query.execute.return_value = _MockResult([])
        with self._setup_patch("services.execution_event_service.get_supabase"):
            from services.execution_event_service import ExecutionEventService
            import asyncio
            asyncio.run(
                ExecutionEventService.execution_completed("job-1", trace_id="t-1", success=True, summary="Done")
            )
            call_args = self.mock_table.insert.call_args[0][0]
            self.assertEqual(call_args["event_type"], "execution_completed")
            self.assertEqual(call_args["payload"]["success"], True)

    def test_execution_failed_event(self):
        self.mock_query.execute.return_value = _MockResult([])
        with self._setup_patch("services.execution_event_service.get_supabase"):
            from services.execution_event_service import ExecutionEventService
            import asyncio
            asyncio.run(
                ExecutionEventService.execution_failed("job-1", trace_id="t-1", reason="timeout", error="Timed out")
            )
            call_args = self.mock_table.insert.call_args[0][0]
            self.assertEqual(call_args["event_type"], "execution_failed")
            self.assertEqual(call_args["payload"]["error"], "Timed out")


# =============================================================================
# JobRecovery Tests
# =============================================================================


class JobRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.mock_table, self.mock_query = _make_mock()

    def _setup_patch(self, target):
        return patch(target, return_value=MagicMock(table=MagicMock(return_value=self.mock_table)))

    def test_detect_unfinished_jobs_returns_list(self):
        self.mock_query.execute.return_value = _MockResult([{"id": "job-1", "status": "running"}])
        with self._setup_patch("services.job_recovery.get_supabase"):
            from services.job_recovery import JobRecovery
            jobs = JobRecovery.detect_unfinished_jobs()
            self.assertEqual(len(jobs), 1)
            self.assertEqual(jobs[0]["id"], "job-1")

    def test_detect_unfinished_jobs_returns_empty_on_error(self):
        self.mock_query.execute.side_effect = Exception("DB error")
        with self._setup_patch("services.job_recovery.get_supabase"):
            from services.job_recovery import JobRecovery
            jobs = JobRecovery.detect_unfinished_jobs()
            self.assertEqual(jobs, [])

    def test_mark_job_orphaned_updates_status(self):
        self.mock_query.execute.return_value = _MockResult([])
        with self._setup_patch("services.job_recovery.get_supabase"):
            from services.job_recovery import JobRecovery
            ok = JobRecovery.mark_job_orphaned("job-1", reason="timeout")
            self.assertTrue(ok)
            # Check first call is jobs update, second is job_state_transitions insert
            table_calls = [c[0][0] for c in self.mock_table.method_calls if c[0] == 'update']
            self.assertTrue(any(c[0] == 'update' for c in self.mock_table.method_calls))
            # Verify the update payload contains correct status
            update_payload = self.mock_table.update.call_args[0][0]
            self.assertEqual(update_payload["status"], "failed")
            self.assertFalse(update_payload["recoverable"])
            self.assertIn("Orphaned", update_payload["recovery_reason"])

    def test_mark_job_recoverable_resets_status(self):
        self.mock_query.execute.return_value = _MockResult([])
        with self._setup_patch("services.job_recovery.get_supabase"):
            from services.job_recovery import JobRecovery
            ok = JobRecovery.mark_job_recoverable("job-1", reason="detected_orphaned")
            self.assertTrue(ok)
            update_payload = self.mock_table.update.call_args[0][0]
            self.assertEqual(update_payload["status"], "queued")
            self.assertTrue(update_payload["recoverable"])

    def test_generate_recovery_report(self):
        self.mock_query.execute.return_value = _MockResult([])
        with self._setup_patch("services.job_recovery.get_supabase"):
            from services.job_recovery import JobRecovery
            report = JobRecovery.generate_recovery_report()
            self.assertIn("unfinished", report)
            self.assertIn("orphaned", report)
            self.assertIn("recoverable", report)
            self.assertIn("recommendations", report)
            self.assertEqual(report["unfinished"]["count"], 0)

    def test_list_recoverable_jobs(self):
        self.mock_query.execute.return_value = _MockResult([{"id": "job-1", "recoverable": True}])
        with self._setup_patch("services.job_recovery.get_supabase"):
            from services.job_recovery import JobRecovery
            jobs = JobRecovery.list_recoverable_jobs()
            self.assertEqual(len(jobs), 1)
            self.assertTrue(jobs[0]["recoverable"])


# =============================================================================
# ExecutionAudit Tests
# =============================================================================


class ExecutionAuditTests(unittest.TestCase):
    def setUp(self):
        self.mock_table, self.mock_query = _make_mock()

    def _setup_patch(self, target):
        return patch(target, return_value=MagicMock(table=MagicMock(return_value=self.mock_table)))

    def test_get_execution_timeline_returns_job_data(self):
        # First call is job (maybe_single), then 6 more queries for transitions/events/steps/decisions/retries/errors
        results = [
            _MockResult({"id": "job-1", "status": "completed"}),  # job
            _MockResult([]),  # transitions
            _MockResult([]),  # events
            _MockResult([]),  # steps
            _MockResult([]),  # decisions
            _MockResult([]),  # retries
            _MockResult([]),  # errors
        ]
        self.mock_query.execute.side_effect = results
        with self._setup_patch("services.execution_audit.get_supabase"):
            from services.execution_audit import ExecutionAudit
            timeline = ExecutionAudit.get_execution_timeline("job-1")
            self.assertEqual(timeline["job_id"], "job-1")
            self.assertIn("state_transitions", timeline)
            self.assertIn("events", timeline)
            self.assertIn("steps", timeline)
            self.assertIn("decisions", timeline)
            self.assertIn("retries", timeline)
            self.assertIn("errors", timeline)
            self.assertIn("step_count", timeline)
            self.assertIn("can_reconstruct", timeline)

    def test_get_execution_timeline_returns_error_on_db_failure(self):
        self.mock_query.execute.side_effect = Exception("DB error")
        with self._setup_patch("services.execution_audit.get_supabase"):
            from services.execution_audit import ExecutionAudit
            timeline = ExecutionAudit.get_execution_timeline("job-1")
            self.assertIn("error", timeline)
            self.assertEqual(timeline["job_id"], "job-1")


if __name__ == "__main__":
    unittest.main()
