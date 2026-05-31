# ThinkSync Execution Architecture: Worker Migration Path

## Current State (v1.28 - BackgroundTasks)

### Architecture
```
FastAPI HTTP Router
    ↓
BackgroundTasks.add_task()
    ↓
In-process async function run_job()
    ↓
Job execution in memory
    ↓
Status updates to DB/Redis
```

### Reliability Issues
1. **Job loss**: Server restart loses in-flight jobs
2. **Orphaned execution**: No process supervision or heartbeat
3. **No recovery**: Unfinished jobs unmarked as orphaned
4. **State inconsistency**: Job row may not reflect actual execution state
5. **Resource leaks**: No timeout enforcement beyond step_timeout
6. **Visibility**: No way to know if job process is still alive

## Target State (v2.0 - Queue-based Workers)

### Architecture
```
FastAPI HTTP Router
    ↓
Create job row + initial event
    ↓
Enqueue job_id to durable queue
    ↓
Worker pool subscribes to queue
    ↓
Worker processes job
    ↓
Worker publishes execution events
    ↓
Worker marks job complete + final event
    ↓
Frontend polls or streams events
```

### Benefits
1. **Durability**: Jobs persist in queue across restarts
2. **Supervision**: Workers report heartbeat
3. **Recovery**: Unfinished jobs auto-rescheduled
4. **Scalability**: Multiple workers per instance
5. **Visibility**: Job state matches execution reality
6. **Observability**: Every event recorded

## Migration Path

### Phase 1: Foundation (COMPLETED)
- ✅ Create `job_events` table for durable event storage
- ✅ Create `job_state_transitions` table for state tracking
- ✅ Implement `_record_job_event()` function
- ✅ Implement `_record_status_transition()` function
- ✅ Update `_publish()` to persist events to DB
- ✅ Create `ExecutionAudit` service for timeline queries
- ✅ Ensure all events flow through `_publish()` for recording

**Outcome**: Complete audit trail captured even without worker system

### Phase 2: Preparation (FOUNDATION LAID)
- ⚡ Create `JobQueue` service (durable queue abstraction)
  - Abstract interface supporting Redis, Postgres, or external queues
  - Initially implement with Redis sorted sets: `jobs:pending:{priority}`
  - Include job_id, enqueue_time, priority, retries

- ⚡ Create `WorkerHeartbeat` service
  - Track worker identity, status, job_id, last_heartbeat_at
  - Detect stale workers (heartbeat timeout)
  - Auto-mark stale worker jobs as orphaned

- ⚡ Create `JobRecovery` service
  - Periodically scan for orphaned jobs
  - Check job status vs actual completion events
  - Auto-mark recoverable jobs as queued
  - Log recovery events for audit

- ⚡ Audit all `BackgroundTasks.add_task()` call sites:
  - `/routers/jobs.py:26` - submit_job()
  - `/routers/agents.py:147` - forge_v2_run_async()
  - Count: 2 places currently

**Dependencies to prepare**:
- Job queue selection (likely Redis with fallback to Postgres)
- Worker heartbeat interval and timeout thresholds
- Recovery scan interval (suggested: every 5 minutes)
- Maximum retries per job

### Phase 3: Parallel Execution (FUTURE)
- Build new `worker_main.py` entry point
  - Parse settings (instance_id, queue_name, concurrency)
  - Create worker instance
  - Subscribe to queue
  - Process jobs in parallel with semaphore
  - Publish heartbeats
  - Graceful shutdown handling

- Deploy workers alongside FastAPI app
  - Initially co-process with app on same instance
  - Later: separate worker instances

- Run parallel processing:
  - Phase 3a: Workers active but only FastAPI enqueues (safe dual-write)
  - Phase 3b: Both FastAPI and workers enqueue (full dual-write)
  - Phase 3c: Cut over to workers only (sunset BackgroundTasks)

### Phase 4: Full Cutover (FUTURE)
- Remove `BackgroundTasks.add_task()` from routers
- Enforce all job submission through queue
- Sunset in-process job execution
- Monitor for stragglers, recover any remaining

## Implementation Checklist

### Immediate (v1.29 - Next Sprint)
- [ ] Create `services/job_queue.py` with interface definition
  - [ ] `enqueue(job_id: str, priority: int = 0)`
  - [ ] `dequeue() -> str | None`
  - [ ] `acknowledge(job_id: str)`
  - [ ] `requeue(job_id: str)`
  
- [ ] Create `services/worker_heartbeat.py`
  - [ ] `register(worker_id: str, instance_id: str, concurrency: int)`
  - [ ] `heartbeat(worker_id: str, job_id: str | None = None)`
  - [ ] `get_stale_workers(timeout_seconds: int = 30) -> list[str]`

- [ ] Create `services/job_recovery.py`
  - [ ] `find_orphaned_jobs() -> list[str]`
  - [ ] `recover_job(job_id: str, reason: str) -> bool`
  - [ ] `run_recovery_loop()` (async background task)

- [ ] Update app startup to run recovery loop
  - [ ] Add to lifespan context manager in main.py

- [ ] Write tests for recovery detection
  - [ ] Test orphan detection
  - [ ] Test recovery marking
  - [ ] Test timeline reconstruction

### Later (v1.30+)
- [ ] Create `worker_main.py` entry point
- [ ] Implement Redis-based JobQueue (production)
- [ ] Deploy workers in staging
- [ ] Load test parallel workers
- [ ] Implement graceful worker shutdown
- [ ] Monitor and cutover to queue-only

## Risk Mitigation

### During Migration
1. **Dual-write phase**: Both BackgroundTasks and queue receive jobs
   - Risk: Duplicate execution
   - Mitigation: Idempotent job execution, deduplication in queue

2. **Queue failure**: Redis down but FastAPI still running
   - Risk: Jobs lost
   - Mitigation: Fallback to Postgres queue, or to BackgroundTasks temporarily

3. **Worker crash**: Executing job lost
   - Risk: Job never completes
   - Mitigation: Worker heartbeat timeout → job auto-recovered

4. **Uneven load**: Some workers starve
   - Risk: Some jobs delayed
   - Mitigation: Priority queue, fair work distribution

## Success Metrics

After migration:
- ✅ 0 job losses due to server restart
- ✅ All orphaned jobs detected within 5 minutes
- ✅ All orphaned jobs recoverable within 1 minute
- ✅ Complete audit trail for every job
- ✅ Execution timeline reconstructable for debugging
- ✅ Worker pool scales horizontally

## Backward Compatibility

- `BackgroundTasks.add_task()` calls remain in place during Phase 3
- New queue system runs alongside existing system
- Sunset plan: v2.0 (after Q3 2026 stabilization)

## References

- See `services/execution_audit.py` for timeline reconstruction
- See `backend/db/schema.sql` for job_events and job_state_transitions
- See `models/job.py` for JobStatus enum
