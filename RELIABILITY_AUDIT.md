# ThinkSync Reliability Sprint v1 — Code Audit Report

**Audit date:** 2026-05-31
**Method:** Line-by-line code verification against claims. No trust in documentation.
**Auditor:** System

---

## 1. Implemented Correctly

### 1.1 Database Schema (4 new tables + 3 columns)
**Status:** VERIFIED ✅

| Claim | Evidence | Lines |
|-------|----------|-------|
| `job_steps` table | `backend/db/schema.sql` lines 108-126 | 19 lines of DDL |
| `job_decisions` table | `backend/db/schema.sql` lines 128-137 | 10 lines of DDL |
| `job_retries` table | `backend/db/schema.sql` lines 139-148 | 10 lines of DDL |
| `job_execution_details` table | `backend/db/schema.sql` lines 150-157 | 8 lines of DDL |
| `deleted_at` column on `jobs` | `backend/db/schema.sql` line 455 | `add column if not exists deleted_at timestamptz` |
| `recoverable` column on `jobs` | `backend/db/schema.sql` line 456 | `add column if not exists recoverable boolean not null default false` |
| `recovery_reason` column on `jobs` | `backend/db/schema.sql` line 457 | `add column if not exists recovery_reason text` |
| Indexes on new tables | `backend/db/schema.sql` lines 199-210 | 6 indexes |
| RLS policies on new tables | `backend/db/schema.sql` lines 211-242 | 4 policies |

**Migration file:** `backend/db/migrations/20260531_reliability_sprint_v1.sql` — 208 lines, COMPLETE. Matches schema.sql exactly.

---

### 1.2 ExecutionEventService (12 event types)
**Status:** VERIFIED ✅

| Event Type | Method | Line |
|------------|--------|------|
| `job_created` | `job_created()` | `execution_event_service.py:103` |
| `planning_started` | `planning_started()` | `execution_event_service.py:107` |
| `planning_completed` | `planning_completed()` | `execution_event_service.py:111` |
| `execution_started` | `execution_started()` | `execution_event_service.py:115` |
| `step_started` | `step_started()` | `execution_event_service.py:119` |
| `step_completed` | `step_completed()` | `execution_event_service.py:123` |
| `validation_started` | `validation_started()` | `execution_event_service.py:127` |
| `validation_completed` | `validation_completed()` | `execution_event_service.py:131` |
| `retry_started` | `retry_started()` | `execution_event_service.py:135` |
| `retry_completed` | `retry_completed()` | `execution_event_service.py:139` |
| `execution_failed` | `execution_failed()` | `execution_event_service.py:143` |
| `execution_completed` | `execution_completed()` | `execution_event_service.py:147` |
| `state_transition` | `state_transition()` | `execution_event_service.py:151` |

Core `emit()` method: `execution_event_service.py:54` — persists to DB (`job_events`) + Redis (rpush, ltrim, publish, expire). Best-effort: failures logged, never raised.

Sequence numbering: `_next_sequence()` uses Redis INCR with local fallback (`_LOCAL_SEQ` dict). Verified.

---

### 1.3 ExecutionRepository (dual-write)
**Status:** VERIFIED ✅

| Function | File | Line | Action |
|----------|------|------|--------|
| `save_step()` | `execution_repository.py` | 24 | Inserts to `job_steps` |
| `save_steps()` | `execution_repository.py` | 57 | Bulk insert to `job_steps` + updates `jobs.steps` JSONB |
| `get_steps()` | `execution_repository.py` | 108 | Reads from `job_steps` |
| `save_decision()` | `execution_repository.py` | 130 | Inserts to `job_decisions` |
| `save_decisions()` | `execution_repository.py` | 152 | Bulk insert to `job_decisions` + updates `jobs.decisions` JSONB |
| `get_decisions()` | `execution_repository.py` | 186 | Reads from `job_decisions` |
| `save_retry()` | `execution_repository.py` | 208 | Inserts to `job_retries` |
| `save_retries()` | `execution_repository.py` | 233 | Bulk insert to `job_retries` + updates `jobs.retries` JSONB |
| `get_retries()` | `execution_repository.py` | 253 | Reads from `job_retries` |
| `save_execution_detail()` | `execution_repository.py` | 275 | Inserts to `job_execution_details` |
| `save_errors()` | `execution_repository.py` | 299 | Bulk insert to `job_execution_details` (type=error) + updates `jobs.errors` JSONB |
| `get_execution_details()` | `execution_repository.py` | 325 | Reads from `job_execution_details` with optional type filter |
| `reconstruct_job_execution()` | `execution_repository.py` | 351 | Aggregates all 5 detail types |

All functions are best-effort: try/except/log/return-bool. Never raise.

---

### 1.4 Integration in agent_service.py
**Status:** VERIFIED ✅

| Integration Point | Event/Action | Line | Code |
|-------------------|-------------|------|------|
| `create_job` | `job_created` event | `agent_service.py:~1790` | `asyncio.create_task(ExecutionEventService.job_created(...))` |
| `run_agent_pipeline` (planning) | `planning_started` + `planning_completed` | `agent_service.py:1419,1430` | `await ExecutionEventService.planning_started(...)` then `await ExecutionEventService.planning_completed(...)` |
| `run_agent_pipeline` (start) | `state_transition` + `execution_started` | `agent_service.py:1176-1180` | `await ExecutionEventService.state_transition(...)` then `await ExecutionEventService.execution_started(...)` |
| `on_step_result` | `save_step` | `agent_service.py:1493` | `save_step(job_id, result)` — synchronous, best-effort |
| `on_decision` | `save_decision` | `agent_service.py:1517` | `save_decision(job_id, decision, step_number=...)` — synchronous, best-effort |
| `run_agent_pipeline` (end) | `state_transition` + `execution_completed`/`execution_failed` | `agent_service.py:1694-1705` | `await ExecutionEventService.state_transition(...)` then completion/failure event |

---

### 1.5 Integration in executor.py
**Status:** VERIFIED ✅

| Integration Point | Action | Line | Code |
|---------------------|--------|------|------|
| Retry loop | `save_retry` | `executor.py:~743` | `save_retry(job_id=job_id, step_number=step.step, ...)` — guarded by try/except, job_id check |
| Success contract | `execution_started` event | `executor.py:~797` | `asyncio.create_task(ExecutionEventService.execution_started(...))` — guarded by try/except |

---

### 1.6 JobQueue (architecture foundation)
**Status:** VERIFIED ✅

| Method | File | Line | Purpose |
|--------|------|------|---------|
| `enqueue_job()` | `job_queue.py` | 65 | Redis hset for pending tracking |
| `claim_job()` | `job_queue.py` | 81 | Redis SET NX lock + heartbeat |
| `heartbeat()` | `job_queue.py` | 106 | Update heartbeat with owner check |
| `release_job()` | `job_queue.py` | 127 | Delete lock + heartbeat |
| `is_job_claimed()` | `job_queue.py` | 146 | Check Redis EXISTS |
| `is_job_heartbeat_stale()` | `job_queue.py` | 157 | Check age against heartbeat timestamp |
| `list_active_jobs()` | `job_queue.py` | 177 | DB query for active statuses with `deleted_at is null` |
| `audit_background_tasks()` | `job_queue.py` | 203 | Static diagnostic report with 4 findings |

---

### 1.7 JobRecovery (detection + marking)
**Status:** VERIFIED ✅

| Method | File | Line | Purpose |
|--------|------|------|---------|
| `detect_unfinished_jobs()` | `job_recovery.py` | 40 | Query `jobs` where status in active + `deleted_at is null` + `created_at > cutoff` |
| `detect_orphaned_jobs()` | `job_recovery.py` | 65 | Query stale jobs + Redis heartbeat cross-check |
| `detect_orphaned_without_redis()` | `job_recovery.py` | 99 | DB-only fallback |
| `mark_job_orphaned()` | `job_recovery.py` | 126 | Update `jobs` status=failed, recoverable=false + insert state_transition + emit event |
| `mark_job_recoverable()` | `job_recovery.py` | 170 | Update `jobs` status=queued, recoverable=true + insert state_transition |
| `list_recoverable_jobs()` | `job_recovery.py` | 206 | Query `jobs` where `recoverable=true` |
| `generate_recovery_report()` | `job_recovery.py` | 231 | Aggregates all 4 detection methods |
| `batch_mark_recoverable()` | `job_recovery.py` | 272 | Iterate orphaned + mark each |
| `batch_mark_orphaned()` | `job_recovery.py` | 297 | Iterate orphaned + mark each as failed |

---

### 1.8 API Endpoints
**Status:** VERIFIED ✅

All 8 endpoints exist in `backend/routers/jobs.py`:

| Endpoint | Method | Line | Auth | Description |
|----------|--------|------|------|-------------|
| `/{job_id}/timeline` | GET | 56 | `get_current_user` | Uses `ExecutionAudit.get_execution_timeline()` |
| `/{job_id}/steps` | GET | 74 | `get_current_user` | Uses `get_steps()` |
| `/{job_id}/decisions` | GET | 87 | `get_current_user` | Uses `get_decisions()` |
| `/{job_id}/retries` | GET | 100 | `get_current_user` | Uses `get_retries()` |
| `/{job_id}/errors` | GET | 113 | `get_current_user` | Uses `get_execution_details(detail_type="error")` |
| `/recovery/report` | GET | 131 | `get_current_user` | Uses `JobRecovery.generate_recovery_report()` |
| `/recovery/{job_id}/mark-recoverable` | POST | 144 | `get_current_user` | Uses `JobRecovery.mark_job_recoverable()` |
| `/recovery/{job_id}/mark-orphaned` | POST | 157 | `get_current_user` | Uses `JobRecovery.mark_job_orphaned()` |

Router is included in `backend/main.py` line 137: `app.include_router(jobs.router)` — VERIFIED.

---

### 1.9 ExecutionAudit Enhancement
**Status:** VERIFIED ✅

`ExecutionAudit.get_execution_timeline()` in `execution_audit.py` now queries:
- `job_state_transitions` (was already there)
- `job_events` (was already there)
- `job_steps` (NEW)
- `job_decisions` (NEW)
- `job_retries` (NEW)
- `job_execution_details` where `detail_type="error"` (NEW)

Returns additional fields: `steps`, `decisions`, `retries`, `errors`, `step_count`, `decision_count`, `retry_count`, `error_count`, `can_reconstruct`.

---

### 1.10 Tests
**Status:** VERIFIED ✅

`backend/tests/test_reliability_sprint.py` — 22 tests, ALL PASS:
- 8 ExecutionRepository tests (save_step, save_decision, save_retry, save_detail, get_steps, reconstruct, error handling)
- 7 ExecutionEventService tests (emit, state_transition, job_created, execution_started, execution_completed, execution_failed)
- 6 JobRecovery tests (detect_unfinished, detect_orphaned, mark_orphaned, mark_recoverable, generate_report, list_recoverable)
- 2 ExecutionAudit tests (get_timeline, error handling)

Run result: `22 passed in 3.90s` — VERIFIED.

---

## 2. Partially Implemented

### 2.1 Event emission: `step_started`, `validation_started`, `validation_completed`, `retry_started`, `retry_completed`
**Status:** PARTIAL ⚠️

**What exists:** Methods are defined in `ExecutionEventService` (lines 119, 127, 131, 135, 139).
**What is missing:** These events are NEVER called from anywhere in the codebase.

Verification:
- `grep -r "step_started\|validation_started\|validation_completed\|retry_started\|retry_completed" backend/`
- Only matches: `execution_event_service.py` (definitions only)
- No matches in: `agent_service.py`, `executor.py`, `planner.py`, any router

**Impact:** The 5 event types are ready but dormant. They will never fire unless someone explicitly calls them. The `step_completed` event IS fired (via `on_step_result` → `save_step` which does NOT emit events — the events are only emitted via `execution_started/execution_failed/execution_completed` at pipeline boundaries). Actually, `step_completed` is also not explicitly called via the event service — only `save_step` persists to the DB table.

**Conclusion:** `step_completed` event is NOT emitted. The events that ARE emitted are: `job_created`, `planning_started`, `planning_completed`, `execution_started`, `state_transition` (twice), `execution_failed`, `execution_completed`. That's 7-8 distinct event types. The 12 claimed include 5 that are defined but not wired.

---

### 2.2 `save_errors` in agent_service.py
**Status:** PARTIAL ⚠️

**What exists:** `save_errors()` function in `execution_repository.py` (line 299).
**What is missing:** `save_errors()` is NEVER called from `agent_service.py` or `executor.py`. The errors are still only written to the `jobs.errors` JSONB column via the existing `_db_update` calls.

Verification:
- `grep -r "save_errors\|save_execution_detail" backend/services/agent_service.py backend/services/executor.py`
- Zero matches.

**Impact:** Errors are NOT persisted to `job_execution_details`. The `get_execution_details(job_id, detail_type="error")` endpoint will return empty for all jobs until `save_errors` is wired.

---

### 2.3 `save_steps` and `save_decisions` bulk functions
**Status:** PARTIAL ⚠️

**What exists:** `save_steps()` and `save_decisions()` functions exist.
**What is missing:** They are never called. Only `save_step()` (singular) and `save_decision()` (singular) are called from `on_step_result` and `on_decision`.

**Impact:** Minor. The singular functions handle one record at a time. The bulk functions were designed for final-save optimization, but the per-step approach is fine.

---

### 2.4 `deleted_at` filtering in `get_job`/`list_jobs`
**Status:** PARTIAL ⚠️

**What exists:** `deleted_at` column exists in schema. `list_active_jobs()` in `JobQueue` uses `.is_("deleted_at", "null")`.
**What is missing:** `AgentService.get_job()` and `AgentService.list_jobs()` do NOT filter on `deleted_at`. They will return soft-deleted jobs.

Verification:
- `get_job()` in `agent_service.py` line 1822: `get_supabase().table(_TABLE).select("*").eq("id", job_id).eq("user_id", user_id)` — no `deleted_at` filter.
- `list_jobs()` in `agent_service.py` line 1844: `query = get_supabase().table(_TABLE).select("*").eq("user_id", user_id).eq("workspace_id", workspace_id)` — no `deleted_at` filter.

**Impact:** Soft-delete is schema-level only. The application layer doesn't respect it yet.

---

### 2.5 `get_job`/`list_jobs` enrichment from new tables
**Status:** PARTIAL ⚠️

**What was claimed:** "Update get_job/list_jobs to enrich from new tables"
**What exists:** `get_job()` and `list_jobs()` still return `_row_to_response(result.data)` which only uses the `jobs` table JSONB columns.

**What is missing:** They do NOT join or query `job_steps`, `job_decisions`, etc. to enrich the response. The separate endpoints (`/steps`, `/decisions`, etc.) exist, but the main `get_job` endpoint doesn't include them.

**Impact:** To get the full execution picture, a client must call `get_job` + `get steps` + `get decisions` + `get retries` + `get errors`. The timeline endpoint (`/timeline`) does this, but the main `get_job` is unchanged.

---

## 3. Missing

### 3.1 `step_started` event emission
**Status:** MISSING ❌

Method exists but is never called. No code in `executor.py` or `agent_service.py` calls `ExecutionEventService.step_started()`.

**Where it should be:** In `executor.py` around line 512-517 where `on_step_start` is called:
```python
if on_step_start:
    try:
        await on_step_start(step.step, tool_name, step.args)
    except Exception:
        pass
```
This is where `ExecutionEventService.step_started()` should be emitted.

---

### 3.2 `validation_started` and `validation_completed` event emission
**Status:** MISSING ❌

Methods exist but never called. No code in `executor.py` calls these events.

**Where it should be:** In `executor.py` in `_validate_result()` (around line 590-620), before and after the validator loop.

---

### 3.3 `retry_started` and `retry_completed` event emission
**Status:** MISSING ❌

Methods exist but never called. The retry loop in `executor.py` (line 734) only calls `save_retry()` (DB persistence) but never emits the event.

**Where it should be:** In the retry loop, before `await asyncio.sleep(...)` and after the retry result.

---

### 3.4 `step_completed` event emission
**Status:** MISSING ❌

Method exists but never called. `save_step()` writes to `job_steps` table but does NOT emit an event. The `on_step_result` callback in `agent_service.py` only calls `save_step()` and `_publish()` (WebSocket) but not `ExecutionEventService.step_completed()`.

**Where it should be:** In `on_step_result` after `save_step()`:
```python
await ExecutionEventService.step_completed(job_id, result.step, result.tool.value, success=result.success, exit_code=result.exit_code, validation_passed=result.validation_passed)
```

---

### 3.5 `save_errors` wired into failure paths
**Status:** MISSING ❌

`save_errors()` is defined but never called. When `run_agent_pipeline` fails (line 1348-1360), it does `_db_update(job_id, {"status": JobStatus.FAILED.value, "summary": summary})` but never calls `save_errors()` to persist to `job_execution_details`.

**Where it should be:** In the exception handler at line 1348-1360 and in the final save block at line 1635-1648.

---

### 3.6 `save_execution_detail` for metadata/analysis/contract
**Status:** MISSING ❌

Function exists but never called. No detail_type values other than "error" are ever written.

**Where it could be:** In `executor.py` success contract section, or in the constitution engine.

---

### 3.7 `JobQueue.claim_job` called in `run_job`
**Status:** MISSING ❌

`JobQueue.claim_job()` is defined but never called. `AgentService.run_job()` (line 1884) just calls `await _run_agent_loop()` without claiming the job via the queue.

**Impact:** The queue architecture is purely conceptual. The `claim_job`, `heartbeat`, `release_job` methods are unused. The existing Redis workspace lock (`forge:lock:{workspace_id}`) is still used instead.

---

### 3.8 `JobQueue.heartbeat` called during execution
**Status:** MISSING ❌

Never called. No periodic heartbeat during job execution.

---

### 3.9 `JobQueue.release_job` called after completion
**Status:** MISSING ❌

Never called. `run_agent_pipeline` has no finally block that releases the job.

---

### 3.10 `JobQueue.enqueue_job` called in router
**Status:** MISSING ❌

The `submit_job` router in `jobs.py` still uses `background_tasks.add_task(AgentService.run_job, ...)` without calling `JobQueue.enqueue_job()`.

---

### 3.11 Recovery endpoint: automatic batch on startup
**Status:** MISSING ❌

No code calls `batch_mark_recoverable()` or `batch_mark_orphaned()` on server startup. The `detect_orphaned_jobs()` is only called via the manual API endpoint.

**Where it should be:** In a startup event or background task in `main.py`.

---

### 3.12 `get_job`/`list_jobs` soft-delete filtering
**Status:** MISSING ❌

`deleted_at` column exists but is never queried in the main job endpoints.

---

## 4. Architectural Concerns

### 4.1 `asyncio.create_task()` for events without awaiting
**Severity:** MEDIUM

In `create_job` (line 1790) and `executor.py` (line 802), events are fired via `asyncio.create_task()` inside a try/except block. This is correct for fire-and-forget, but:
- The task is not tracked — if the event loop shuts down before it completes, the event may be lost
- No `await` means the caller doesn't wait for DB persistence
- In `create_job`, `ExecutionEventService.job_created()` is an async function but the `create_job` method is SYNC, so it uses `asyncio.create_task()` — this works if there's an event loop, but if called from a non-asyncio context, it may fail silently

**Recommendation:** For sync contexts, consider using a background thread or a fire-and-forget queue.

### 4.2 `_db_update` and `save_step` race condition
**Severity:** LOW

In `on_step_result`:
```python
_db_update(job_id, {"steps": accumulated_steps, ...})  # updates jobs.steps JSONB
save_step(job_id, result)  # inserts into job_steps
```

These are two separate DB calls with no transaction. If the first fails and the second succeeds, the JSONB is stale but the normalized table is correct. If the second fails, the JSONB is correct but the normalized table is missing the step.

**Recommendation:** Wrap in a transaction or accept eventual consistency.

### 4.3 `execution_started` emitted TWICE per job
**Severity:** LOW

- `agent_service.py` line 1180: `ExecutionEventService.execution_started()` at pipeline start
- `executor.py` line 802: `ExecutionEventService.execution_started()` in success contract

This means every job gets TWO `execution_started` events. One is correct (pipeline start), one is in the wrong place (success contract section should probably be something else, or should be removed).

### 4.4 `executor.py` emits event at wrong location
**Severity:** LOW

The `execution_started` event in `executor.py` line 797 is inside the success contract section, which runs AFTER all steps are executed. This is semantically wrong — `execution_started` should fire at the beginning of execution, not after it.

### 4.5 `on_decision` calls `save_decision` with synchronous function in async context
**Severity:** LOW

`save_decision()` is a synchronous function that does a blocking DB call (`get_supabase().table(...).insert(...).execute()`). In `on_decision` (an async callback), it is called without `await`:
```python
save_decision(job_id, decision, step_number=len(accumulated_steps))
```

This blocks the event loop. While the call is best-effort (no await), it still blocks the async thread. The same issue exists for `save_step()` in `on_step_result`.

**Recommendation:** Make `save_step` and `save_decision` async and await them, or run them in a thread pool.

### 4.6 `JobQueue.is_job_heartbeat_stale` uses `datetime.fromisoformat()` on raw bytes
**Severity:** MEDIUM

```python
heartbeat = redis.get(heartbeat_key)
if not heartbeat:
    return True
last_beat = datetime.fromisoformat(heartbeat.decode())  # may fail if format is wrong
```

If the heartbeat value is malformed (e.g., a different string was written by a different system), `fromisoformat()` will throw an exception, which is caught and returns True (stale). This is correct behavior but could mask real issues.

### 4.7 `detect_orphaned_jobs` uses `hours * 3600` for Redis stale check but DB uses `hours` for `updated_at`
**Severity:** LOW

```python
# DB: updated_at older than N hours
.lt("updated_at", cutoff)  # cutoff = now - timedelta(hours=hours)

# Redis: heartbeat stale if older than N * 3600 seconds
JobQueue.is_job_heartbeat_stale(job_id, max_seconds=hours * 3600)
```

This is consistent but the Redis check is much more sensitive (checks seconds-level freshness). If the heartbeat is written once per minute, a 1-hour threshold means 3600 seconds. This is reasonable.

### 4.8 `get_job` endpoint returns `not job` check but `get_job` raises 404
**Severity:** LOW

In `get_job_timeline` and other endpoints:
```python
job = AgentService.get_job(job_id=job_id, user_id=current_user["sub"])
if not job:
    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, ...)
```

But `AgentService.get_job()` already raises `HTTPException(404)` if the job is not found. The `if not job` check is unreachable dead code. This is a minor issue but doesn't cause bugs.

### 4.9 `schema.sql` includes both `create table` AND `alter table` for the same columns
**Severity:** LOW

`schema.sql` has `create table if not exists public.jobs (...)` at the top (lines 59-79) without `deleted_at`, `recoverable`, `recovery_reason`. Then at the bottom (lines 455-457), it has `alter table public.jobs add column if not exists ...` for these columns. This is correct for migration idempotency but the schema is split.

### 4.10 `job_execution_details` table has `CHECK` constraint but `save_execution_detail` doesn't validate
**Severity:** LOW

```sql
detail_type text not null check (detail_type in ('error','metadata','analysis','contract'))
```

`save_execution_detail()` accepts any string. If an invalid `detail_type` is passed, the DB will reject the insert. This is fine (fail at DB level), but the function could pre-validate for better error messages.

---

## 5. Production Risks

### 5.1 Migration NOT applied yet
**Risk:** HIGH

The migration file `20260531_reliability_sprint_v1.sql` exists but has NOT been applied to production. The `schema.sql` changes are in the file but not in the live database.

**Impact:** All new code (save_step, save_decision, etc.) will fail with "relation does not exist" errors when trying to write to `job_steps`, `job_decisions`, etc.

**Mitigation:** Apply the migration before deploying the new code.

### 5.2 Redis URL async parse failure
**Risk:** MEDIUM

The `replit.md` notes: "Async Redis client cannot parse the current REDIS_URL (Upstash REST-token format). Sync client works. Health checker and rate limiter degrade gracefully."

`ExecutionEventService._next_sequence()` uses `RedisService.get_async_client()`. If the async client fails to parse the URL, it returns None, and the local sequence fallback is used. The Redis publish (rpush, ltrim, publish) will also fail silently. This means:
- Events are still persisted to DB (good)
- Events are NOT streamed to Redis (bad for live streaming)
- Sequence numbers are local-only (not shared across instances)

**Mitigation:** Fix the Redis URL format or accept the local fallback.

### 5.3 `asyncio.create_task` in sync `create_job` method
**Risk:** MEDIUM

`AgentService.create_job()` is a synchronous method. Inside it, `asyncio.create_task(ExecutionEventService.job_created(...))` is called. If there is no running event loop (e.g., in a background thread or in a test without asyncio), this will throw `RuntimeError: no running event loop`. The try/except catches it, but the `job_created` event is lost.

**Mitigation:** For sync contexts, use a thread-safe event queue or a background task to emit events.

### 5.4 `save_step` and `save_decision` do not await DB calls
**Risk:** MEDIUM

In async callbacks (`on_step_result`, `on_decision`), synchronous DB calls block the event loop:
```python
save_step(job_id, result)  # sync DB call
save_decision(job_id, decision, ...)  # sync DB call
```

Under high load, these synchronous calls can block the asyncio event loop, reducing concurrency and potentially causing timeouts for other jobs.

**Mitigation:** Wrap in `asyncio.to_thread()` or make the functions async.

### 5.5 BackgroundTasks still used for job execution
**Risk:** HIGH

The `submit_job` endpoint still uses `background_tasks.add_task(AgentService.run_job, ...)` (line 26 of `jobs.py`). The `JobQueue` is a conceptual architecture but not used. Jobs are still lost on server restart.

**Mitigation:** This is documented as a known issue. The sprint was Phase 1 (architecture foundation), not a full migration. Phase 2 would replace BackgroundTasks with the queue.

### 5.6 `job_execution_details` table has `payload` as JSONB but no indexing on payload fields
**Risk:** LOW

If `payload` contains large objects (e.g., full error traces), queries filtering by payload fields will be slow. The current index is on `(job_id, detail_type, created_at)` which is correct for the primary access pattern.

### 5.7 `batch_mark_recoverable` and `batch_mark_orphaned` are manual-only
**Risk:** MEDIUM

These methods are exposed via API but not called automatically. An operator must manually trigger them. If the operator forgets, orphaned jobs accumulate.

**Mitigation:** Add a scheduled background task or cron job to auto-detect and mark orphans.

### 5.8 `get_execution_timeline` does 7 sequential DB queries
**Risk:** MEDIUM

```python
job_result = get_supabase().table("jobs").select("*").eq("id", job_id).maybe_single().execute()
transitions_result = get_supabase().table("job_state_transitions").select("*").eq("job_id", job_id).order("created_at", desc=False).execute()
events_result = get_supabase().table("job_events").select("*").eq("job_id", job_id).order("sequence", desc=False).execute()
steps_result = get_supabase().table("job_steps").select("*").eq("job_id", job_id).order("step_number", desc=False).execute()
decisions_result = get_supabase().table("job_decisions").select("*").eq("job_id", job_id).order("created_at", desc=False).execute()
retries_result = get_supabase().table("job_retries").select("*").eq("job_id", job_id).order("created_at", desc=False).execute()
errors_result = get_supabase().table("job_execution_details").select("*").eq("job_id", job_id).eq("detail_type", "error").order("created_at", desc=False).execute()
```

7 sequential round-trips to the database. Under load, this endpoint will be slow. Each query is independent and could be parallelized.

**Mitigation:** Use `asyncio.gather()` or a single JOIN query.

### 5.9 `save_step` truncates stdout/stderr to 6000/3000 chars
**Risk:** LOW

```python
"stdout": (result.stdout or "")[:6000],
"stderr": (result.stderr or "")[:3000],
```

This is correct for preventing large writes, but it means the full output is NOT preserved in the normalized table. The full output is still in the `StepResult` object and in the JSONB `jobs.steps` column (which also gets truncated by the same `[:6000]` in the existing code).

**Mitigation:** Consider storing full output in a separate storage (S3, file system) and linking to it.

### 5.10 `planning_completed` event passes `plan` list but may be empty for non-LLM plans
**Risk:** LOW

```python
await ExecutionEventService.planning_completed(job_id, trace_id=trace_id, plan=[s.model_dump(mode="json") for s in planned_plan] if isinstance(planned_plan, list) else [])
```

If `planned_plan` is not a list of `AgentStep` objects (e.g., a simple plan from `_default_non_server_plan`), the `s.model_dump(mode="json")` call will fail with AttributeError. However, the `isinstance(planned_plan, list)` check handles the basic case. The issue is if `planned_plan` is a list of dicts instead of `AgentStep` objects.

**Mitigation:** Add a type check or use `getattr(s, "model_dump", lambda **kw: dict(s))`.

---

## Summary

| Category | Count | Details |
|----------|-------|---------|
| Implemented correctly | 10 | Schema, 8 event methods, repository, 8 API endpoints, 4 integration points, audit, tests |
| Partially implemented | 5 | 5 event types defined but not wired, save_errors not wired, bulk functions unused, soft-delete not filtered, enrichment not in main endpoints |
| Missing | 12 | step_started, validation_started/completed, retry_started/completed, step_completed events, save_errors wiring, save_execution_detail wiring, claim_job/heartbeat/release_job usage, enqueue_job usage, auto-recovery on startup, soft-delete filtering |
| Architectural concerns | 10 | Fire-and-forget tasks, race conditions, duplicate events, sync-in-async blocking, heartbeat parsing, orphan detection sensitivity, dead code, schema split, DB constraint validation |
| Production risks | 10 | Migration not applied, Redis URL failure, sync event emission, event loop blocking, BackgroundTasks still used, payload indexing, manual-only recovery, 7 sequential DB queries, truncation, plan type safety |

**Overall Assessment:** The architecture is solid, the schema is correct, and the dual-write pattern is well-implemented. The main gaps are:
1. **Event wiring**: 5 of 12 event types are defined but never called
2. **Queue integration**: JobQueue is architecture-only, never used in production paths
3. **Error persistence**: `save_errors` and `save_execution_detail` are not wired
4. **Soft-delete**: Schema exists but not used in queries
5. **Performance**: `get_execution_timeline` does 7 sequential queries

The code is production-ready IF the migration is applied and the missing wiring is completed. The current state is a strong foundation but not a complete implementation.
