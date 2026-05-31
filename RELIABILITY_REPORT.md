# ThinkSync Reliability Sprint v1 — Report

## Summary

The execution architecture has been refactored for durability, auditability, and production readiness without changing any frontend code, auth system, gateway behavior, Redis schema, PM2 architecture, or DB schema beyond adding new tables.

## What Changed

### 1. Database Schema (New Tables)

| Table | Purpose | Rows per job |
|-------|---------|-------------|
| `job_steps` | Durable step records (tool, command, stdout, stderr, exit_code, duration, validation_passed) | 1 per step |
| `job_decisions` | Durable decision records (action, reason, modified_step) | 1 per decision |
| `job_retries` | Durable retry records (step, attempt, command, reason) | 1 per retry |
| `job_execution_details` | Errors, metadata, analysis, contract violations | 1 per error/metadata |

Updated `jobs` table with:
- `deleted_at` — soft-delete support
- `recoverable` boolean — recovery flag
- `recovery_reason` text — why job was recovered

### 2. Execution Event System

All events now emit through `ExecutionEventService` with:
- DB persistence (`job_events` table)
- Redis streaming (`job_events:{job_id}` list + live pub/sub)
- Sequential numbering (Redis INCR with local fallback)
- Best-effort semantics: failures are logged, never raised

Key event types:
- `job_created`, `execution_started`, `execution_completed`, `execution_failed`
- `state_transition` (from → to status with reason)
- `step_started`, `step_completed`, `retry_started`, `retry_completed`

### 3. Job Metadata Separation (Dual-Write)

`ExecutionRepository` provides dual-write to new tables:
- `save_step()` — writes to `job_steps` + keeps JSONB cache
- `save_decision()` — writes to `job_decisions` + keeps JSONB cache
- `save_retry()` — writes to `job_retries` + keeps JSONB cache
- `save_execution_detail()` — writes to `job_execution_details`

**Backward compatibility**: existing JSONB columns on `jobs` are still populated. Reads from new tables can be enabled gradually.

Integration points:
- `on_step_result` callback in `run_agent_pipeline` → `save_step`
- `on_decision` callback in `run_agent_pipeline` → `save_decision`
- Retry loop in `run_server_execution` → `save_retry`
- Execution start/end in `run_agent_pipeline` → `ExecutionEventService`

### 4. Background Task Audit

`JobQueue.audit_background_tasks()` documents all `BackgroundTasks` usage:
- `routers/jobs.py::submit_job` — HIGH risk (job lost on restart)
- `routers/agents.py::forge_v2_run_async` — HIGH risk (same as above)
- `services/agent_service.py::run_job` — MEDIUM risk (in-memory semaphore only)

Mitigation: Job state is tracked in DB and `job_events`. On restart, `detect_orphaned_jobs` can recover.

### 5. Execution Recovery

`JobRecovery` provides:
- `detect_unfinished_jobs()` — all jobs in [queued, running, waiting_for_llm]
- `detect_orphaned_jobs()` — stale jobs with Redis heartbeat cross-check
- `detect_orphaned_without_redis()` — DB-only fallback
- `mark_job_recoverable()` — reset to queued for retry
- `mark_job_orphaned()` — mark as failed
- `generate_recovery_report()` — comprehensive dashboard
- `batch_mark_recoverable()` / `batch_mark_orphaned()` — batch operations

### 6. New API Endpoints

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/jobs/{job_id}/timeline` | GET | Full execution timeline (events + steps + decisions + retries + errors) |
| `/jobs/{job_id}/steps` | GET | Durable step records |
| `/jobs/{job_id}/decisions` | GET | Durable decision records |
| `/jobs/{job_id}/retries` | GET | Durable retry records |
| `/jobs/{job_id}/errors` | GET | Durable error records |
| `/jobs/recovery/report` | GET | Recovery report (counts + recommendations) |
| `/jobs/recovery/{job_id}/mark-recoverable` | POST | Manual recovery |
| `/jobs/recovery/{job_id}/mark-orphaned` | POST | Manual failure |

### 7. ExecutionAudit Enhancement

`ExecutionAudit.get_execution_timeline()` now includes:
- `steps` — from `job_steps`
- `decisions` — from `job_decisions`
- `retries` — from `job_retries`
- `errors` — from `job_execution_details`
- `step_count`, `decision_count`, `retry_count`, `error_count`
- `can_reconstruct` — true if any normalized data exists

## Test Status

- 79 tests pass (pre-existing passing)
- 15 pre-existing failures remain in test files we did not touch
- No new regressions introduced

## Migration

Apply `backend/db/migrations/20260531_reliability_sprint_v1.sql` to production.

The dual-write pattern means existing code continues to work without any changes. The new tables are populated incrementally as new jobs execute.

## Future Work (Phase 2)

- Standalone worker process that polls `JobQueue` instead of `BackgroundTasks`
- Redis-backed distributed queue for horizontal scaling
- Resume/restart support from `job_steps` state
- Worker health checks and auto-restart
