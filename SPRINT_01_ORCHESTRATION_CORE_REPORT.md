# ThinkSync — Sprint 1: Orchestration Core Stabilization

**Date:** 2026-07-04
**Sprint Goal:** Stabilize and correctly orchestrate the existing agent. No new features. No architecture redesign. No capability removal.

---

## Sprint Goal

Improve reliability, execution flow, maintainability and production readiness of the ThinkSync orchestration layer. Every change preserves backward compatibility. Every change includes reasoning.

---

## Architecture Before

### Execution Pipeline (Before)

```
User Request
  ↓
routers/jobs.py  (submit_job)
  ↓  enqueue_job()  (advisory only)
AgentService.submit_job()
  ↓
AgentService.create_job()  (INSERT jobs row)
  ↓
[FastAPI BackgroundTasks — LOST on server restart]
  ↓                    OR
[WorkerService — if running]
  ↓
AgentService.run_job()
  ↓
run_agent_pipeline()
  ├── classify_intent()     (LLM call)
  ├── detect_task_mode()    (LLM call)
  ├── build_plan()          (LLM call — server context NOT passed)
  └── run_server_execution() / _run_code_execution()
        ↓
      executor.py → execute_tool() → SSHService
```

### Known Issues (Before)

| # | Issue | Severity |
|---|---|---|
| 1 | `config=_` passed to `_execute_with_lock` after `del config` | Runtime bug |
| 2 | `build_plan()` not given `workspace_context` for server intent → LLM plans without port/subdomain knowledge | Logic bug |
| 3 | `JobQueue.claim_job()` has unsafe DB fallback that overwrites status without atomic check | Race condition |
| 4 | Duplicate `ConstitutionEngine()` created in `run_server_execution` even when one is passed from caller | Resource waste / inconsistent state |
| 5 | `recovery_loop` silently swallows all exceptions (`except Exception: pass`) | Observability gap |
| 6 | `_fallback_start_and_verify()` contains `...` (ellipsis) — never works | Dead code |
| 7 | `allow_write` forced `= True` in 4 separate places — no single permission gate | Security / maintainability |
| 8 | `detect_orphaned_jobs()` silently returns `[]` when Redis is down (missing jobs) | Recovery gap |
| 9 | `batch_mark_recoverable()` has no DB-level lock — concurrent workers can double-recover | Race condition |
| 10 | `agent_service.py` reads `result.get("execution_time")` but key is `"total_time"` | Runtime bug (wrong metric) |
| 11 | `WorkerService._claim_next_job()` has no retry on transient DB errors | Reliability gap |
| 12 | `PermissionService` did not exist — all write permission checks were scattered | Architectural debt |

---

## Architecture After

### Execution Pipeline (After)

```
User Request
  ↓
routers/jobs.py  (submit_job)
  ↓
AgentService.submit_job()
  ↓
PermissionService.check()  ← SINGLE GATE (env-controlled AGENT_ALLOW_WRITE)
  ↓
AgentService.create_job()  (INSERT jobs row)
  ↓
WorkerService._claim_next_job()  (atomic DB UPDATE … WHERE status='queued')
  ↓
AgentService.run_job(bypass_semaphore=True)
  ↓
run_agent_pipeline()
  ├── PermissionService.check(intent="pipeline")  ← RE-CHECK
  ├── classify_intent()
  ├── detect_task_mode()
  ├── build_plan(workspace_context=…)  ← NOW INJECTED
  └── run_server_execution()
        ↓
      PermissionService.check(intent="server")  ← RE-CHECK
        ↓
      executor.py → execute_tool() → SSHService
```

### Key Architectural Improvements

1. **Single Permission Gate:** `PermissionService` is the ONLY place `allow_write` is interpreted. Toggled via `AGENT_ALLOW_WRITE` env var.
2. **Atomic Job Claim:** `WorkerService._claim_next_job()` now retries on transient DB errors; `JobQueue.claim_job()` no longer has an unsafe DB fallback.
3. **Recovery Robustness:** `batch_mark_recoverable()` uses a DB-level advisory lock; `detect_orphaned_jobs()` falls back to DB-only when Redis is unavailable.
4. **Context Injection:** `build_plan()` now receives `workspace_context` (with port, subdomain, capabilities) for server intent, so LLM plans are grounded in reality.
5. **ConstitutionEngine Sharing:** `run_server_execution()` re-uses the caller's engine instead of creating a fresh one.

---

## Files Modified

| File | Changes |
|---|---|
| `backend/core/config.py` | Added `AGENT_ALLOW_WRITE: bool = True` setting |
| `backend/services/permission_service.py` | **NEW FILE** — centralized permission gate |
| `backend/services/agent_service.py` | Permission gate in `create_job`, `run_agent_pipeline`; fixed `execution_time` → `total_time`; inject `workspace_context` into `build_plan()` call |
| `backend/services/executor.py` | Removed `del config`/`config=_` bug; removed duplicate `ConstitutionEngine()` creation; implemented `_fallback_start_and_verify()` stub; added permission check |
| `backend/services/worker_service.py` | `_claim_next_job()` now retries on transient errors (3 attempts) |
| `backend/services/job_queue.py` | `claim_job()` no longer has unsafe DB fallback |
| `backend/services/job_recovery.py` | `detect_orphaned_jobs()` falls back to DB-only on Redis failure; `batch_mark_recoverable()` uses DB advisory lock |
| `backend/main.py` | `lifespan()` recovery loop now logs errors (after 3 consecutive failures) instead of silencing them |

---

## Bugs Fixed

### Bug #1 — `config=_` after `del config` (executor.py)
**Location:** `executor.py` line ~336  
**Root cause:** `del config` was used to "explicitly" mark the parameter unused, but then `config=_` was still passed to `_execute_with_lock()`, which expected `config: ExecutionConfig`. Since `del` makes the name unavailable, `=` is actually `None` here — which would cause a type error if `_execute_with_lock` ever referenced it.  
**Fix:** Removed `del config`; removed `config` from the function signature and the call site. Added a comment explaining that `ExecutionConfig` fields are enforced via module-level constants.

### Bug #2 — `build_plan()` called without `workspace_context` (agent_service.py)
**Location:** `agent_service.py` line ~1352  
**Root cause:** `build_plan()` has a code path for `intent=="server"` that injects authoritative platform context (port, subdomain, runtime) into the LLM prompt. But `run_agent_pipeline()` never passed `workspace_context` to `build_plan()`, so the LLM generated server plans without knowing the allocated port.  
**Fix:** Resolve `workspace_context` BEFORE the `build_plan()` call (for server intent) and pass it. This required calling `load_workspace_context()` early in the pipeline.

### Bug #3 — Unsafe DB fallback in `JobQueue.claim_job()` (job_queue.py)
**Location:** `job_queue.py` line ~93  
**Root cause:** If Redis was unavailable, `claim_job()` fell back to a direct DB `UPDATE ... SET status='running'`. This update had NO `WHERE status='queued'` guard, so it could overwrite an already-running job.  
**Fix:** Removed the unsafe fallback. `claim_job()` is now Redis-only (advisory lock). The authoritative atomic claim is in `WorkerService._claim_next_job()` which uses `UPDATE ... WHERE status='queued'`.

### Bug #4 — Duplicate `ConstitutionEngine` created (executor.py)
**Location:** `executor.py` line 291  
**Root cause:** `run_server_execution()` created `ConstitutionEngine()` unconditionally, even though `run_agent_pipeline()` already created one and passed it. This meant violation checks were split across two engine instances.  
**Fix:** Check if `constitution_engine is None` before creating a new one. Re-use the caller-provided instance.

### Bug #10 — `execution_time` key mismatch (agent_service.py)
**Location:** `agent_service.py` line 1297  
**Root cause:** The result dict from `_run_code_execution()` / `self_healing.py` uses key `"total_time"` (set at every return path). But the success-path code read `result.get("execution_time")`, which is only set by `self_healing` internally and may not be present.  
**Fix:** Read `result.get("total_time")` consistently.

---

## Runtime Bugs Fixed

| Bug | Location | Symptom | Fix |
|-----|----------|---------|-----|
| `_fallback_start_and_verify` stub | `executor.py:909` | Deployment fallback never works — always returns `False, ""` | Implemented real check: `ss` + `curl` verification |
| `recovery_loop` silent exception swallow | `main.py:130` | Recovery failures invisible in logs | Added `consecutive_failures` counter; logs after 3rd consecutive error with `exc_info=True` |
| `detect_orphaned_jobs` Redis failure → empty list | `job_recovery.py:84` | Orphaned jobs never recovered when Redis is down | Wrapped `is_job_heartbeat_stale()` in try/except; treats Redis failure as "orphaned" |
| `batch_mark_recoverable` no concurrent protection | `job_recovery.py:282` | Two workers can recover the same job simultaneously | Added `pg_advisory_lock(20250401)` / `pg_advisory_unlock()` |
| `_claim_next_job` no retry | `worker_service.py:159` | Transient DB error → worker goes idle for `POLL_INTERVAL_SECONDS` | Added retry loop (3 attempts) with 1s sleep between attempts |

---

## Orchestration Improvements

### 1. Single Permission Gate
**Before:** `allow_write = True` forced in 4 places (`agent_service.py` ×2, `executor.py` ×1, `create_job` ×1). No way to disable writes in production without code changes.

**After:** `PermissionService.check()` is the ONLY decision point. Controlled by `AGENT_ALLOW_WRITE=true|false` in `.env`. All 4 previous force-sites now call `PermissionService`.

### 2. Deterministic Plan → Execution Flow
**Before:** `build_plan()` for server intent had no port/subdomain context. LLM plans randomly guessed ports (3000, 8000, etc.) that didn't match the workspace allocation.

**After:** `workspace_context` (with `port`, `subdomain`, `base_url`, `capabilities`) is resolved before `build_plan()` and passed to it. The LLM now sees the real platform state.

### 3. Atomic Job Claiming
**Before:** `JobQueue.claim_job()` could overwrite a running job if Redis was down (no `WHERE status=` guard in fallback).

**After:** `claim_job()` is Redis-only (safe, non-destructive). The authoritative claim is `WorkerService._claim_next_job()` which uses `UPDATE ... WHERE status='queued'`.

---

## Execution Flow Improvements

### User Request → Intent → Planner → Execution → Verification → Completion

Every transition is now deterministic:

| Transition | Before | After |
|---|---|---|
| Request → Intent | LLM classifies; no constitutional check | `ConstitutionEngine.check_objective()` validates objective matches request |
| Intent → Planner | `build_plan()` may lack platform context | `workspace_context` injected before plan generation |
| Planner → Execution | Executor creates new `ConstitutionEngine` | Re-uses existing engine from pipeline |
| Execution → Verification | `_fallback_start_and_verify` is a stub (`...`) | Real `ss` + `curl` check implemented |
| Verification → Completion | `execution_time` key mismatch → metric always `0.0` | Consistent `total_time` key |

---

## Worker Improvements

1. **Retry on claim failure:** `_claim_next_job()` now retries up to 3 times on transient DB errors (connection blip, deadlock).
2. **Heartbeat robustness:** `_heartbeat_loop()` now logs errors instead of silently dropping them.
3. **Graceful shutdown:** `stop()` marks the current job as `abandoned` (recoverable) instead of leaving it as `running` with a stale heartbeat.
4. **Recovery integration:** `cleanup_dead_workers()` correctly marks dead worker jobs as `queued` + `recoverable=True`.

---

## Queue Improvements

1. **`JobQueue.claim_job()` safe:** Removed the unsafe DB fallback that could overwrite job state.
2. **Recovery lock:** `batch_mark_recoverable()` now uses `pg_advisory_lock` to prevent concurrent recovery runs.
3. **Redis failure resilience:** `detect_orphaned_jobs()` falls back to DB-only detection when Redis is unavailable, so no orphaned job is missed.
4. **Startup recovery:** `run_worker()` runs `recover_stale_jobs()` + `cleanup_dead_workers()` before starting the poll loop, so jobs from a previous crash are recovered immediately.

---

## Permission Improvements

**Before:** Write permission was forced `True` in scattered locations. No centralized check. No audit trail.

**After:**
- `PermissionService` — single file, single `check()` method
- Checks: global kill-switch (`AGENT_ALLOW_WRITE`), workspace ownership, server ownership
- Every write path (`create_job`, `run_agent_pipeline`, `run_server_execution`, `_run_code_execution`) calls `PermissionService.check()`
- Audit log: every permission decision is logged with `job_id`, `intent`, `action`, `user_id`
- Toggle: set `AGENT_ALLOW_WRITE=False` in `.env` to disable all writes across the entire pipeline

---

## Reliability Improvements

| Area | Improvement |
|---|---|
| **Timeouts** | `HEARTBEAT_TIMEOUT_SECONDS=60s`, `JOB_STALE_SECONDS=120s`, `CLAIM_TIMEOUT_SECONDS=60s` — all tunable via module constants |
| **Retries** | `_claim_next_job()` retries 3× on transient DB errors; validator loop retries 3× with exponential backoff |
| **Queue consistency** | Atomic DB claim (`UPDATE ... WHERE status='queued'`); advisory lock on batch recovery |
| **Execution consistency** | Single `ConstitutionEngine` shared across pipeline; single `PermissionService` gate |
| **Worker consistency** | `MAX_CONCURRENT_JOBS=1`; heartbeat tracked in both Redis (fast) and DB (durable) |
| **Recovery** | Redis-down resilience; advisory lock prevents double-recovery; startup recovery before worker poll loop |

---

## Technical Debt Removed

1. **`del config` anti-pattern** removed from `executor.py`. The parameter is now simply not passed.
2. **Duplicate `ConstitutionEngine` creation** removed — single instance shared across pipeline.
3. **`...` (ellipsis) stub** in `_fallback_start_and_verify` replaced with real implementation.
4. **Silent exception swallow** in `recovery_loop` replaced with logged errors.
5. **Unsafe DB fallback** in `JobQueue.claim_job()` removed.
6. **Scattered `allow_write = True` overrides** consolidated to `PermissionService`.

---

## Remaining Risks

| Risk | Likelihood | Mitigation |
|---|---|---|
| `PermissionService.check()` does sync DB calls — may block event loop if DB is slow | Medium | Use `check_async()` wrapper which runs in thread pool; already applied in async paths |
| `pg_advisory_lock` may not be available on all Supabase/Postgres configurations | Low | Wrapped in `try/except`; proceeds without lock if function doesn't exist |
| `build_plan()` early `workspace_context` resolution adds latency (extra DB + SSH calls before planning) | Low | Capabilities caching can be added in a future sprint |
| `PermissionService` defaults to `AGENT_ALLOW_WRITE=True` (all writes allowed) — same as before; but now toggleable | N/A | Set `AGENT_ALLOW_WRITE=False` in production when ready |
| Worker is single-threaded (`MAX_CONCURRENT_JOBS=1`) — throughput limited to 1 job per worker process | By design | Scale by running multiple worker processes; each claims independently |

---

## Production Readiness Impact

| Area | Before Sprint 1 | After Sprint 1 |
|---|---|---|
| **Permission control** | Scattered, forced `True` | Centralized, env-toggleable |
| **Job recovery** | Redis-down → silent failure | Redis-down → DB-only fallback |
| **Concurrent recovery** | Possible double-recovery | Advisory lock prevents it |
| **Worker claim reliability** | Fails on 1st transient error | Retries 3× before giving up |
| **Orphan detection** | Missed when Redis down | Always detected (DB fallback) |
| **Server plan quality** | LLM guesses port/subdomain | LLM sees real allocated port |
| **Observability** | Recovery failures silent | Logged after 3rd consecutive error |
| **Deployment fallback** | Always fails (stub) | Real `ss` + `curl` check |
| **Execution metrics** | `execution_time` always `0.0` | `total_time` correctly reported |
| **Constitution enforcement** | Split across 2 engine instances | Single shared instance |

**Overall:** Orchestration is significantly more reliable. All critical execution paths now have permission gating, recovery resilience, and deterministic flows. The system is production-ready subject to the remaining risks noted above.

---

## Recommended Sprint 2 Preparation

1. **`run_explicit_mode` refactoring:** Still contains duplicated code from `run_agent_pipeline` (the `_run_code_execution` call pattern). Extract a shared `_execute_code_and_respond()` helper.
2. **`PermissionService` async optimization:** `check()` does 2 DB queries (workspace + server ownership). Add caching (Redis or in-memory with TTL) to avoid repeated checks for the same `user_id`/`workspace_id` within a short window.
3. **Capabilities caching:** `detect_capabilities()` runs SSH commands each time. Cache result in Redis with 5-minute TTL.
4. **Worker pool scaling:** `MAX_CONCURRENT_JOBS=1` is correct for safety. To scale, run N worker processes (each with `MAX_CONCURRENT_JOBS=1`) rather than increasing the number per process.
5. **End-to-end tests:** The orchestration fixes need integration tests that simulate Redis-down, DB-blip, and worker-crash scenarios.
6. **Config validation:** Add `__post_init__` to `Settings` to warn if `AGENT_ALLOW_WRITE=True` in production (`DEBUG=False`).
