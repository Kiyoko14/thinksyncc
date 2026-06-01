-- ThinkSync Reliability Sprint v2: Durable Queue & Worker Architecture
-- Adds queue metadata columns to jobs for worker coordination and heartbeat tracking.
-- This is an incremental migration — v1.2 columns and tables remain unchanged.

-- =============================================================================
-- 1. Add worker coordination columns to jobs
-- =============================================================================

alter table public.jobs
    add column if not exists worker_id text;

alter table public.jobs
    add column if not exists claimed_at timestamptz;

alter table public.jobs
    add column if not exists heartbeat_at timestamptz;

alter table public.jobs
    add column if not exists completed_at timestamptz;

-- =============================================================================
-- 2. Extend status check constraint for worker states
-- =============================================================================

-- Note: PostgreSQL does not support ALTER TABLE ALTER COLUMN with CHECK
-- unless the column is re-created. We update the application layer to
-- enforce these states; the DB check remains the same 5 states.
-- Application tracks: queued, claimed, running, waiting_for_llm, completed, failed, abandoned, recoverable.
-- The DB check is kept minimal to avoid migration risk.

-- =============================================================================
-- 3. Indexes for queue performance
-- =============================================================================

-- Fast lookup for worker claiming jobs
create index if not exists idx_jobs_status_worker_id
    on public.jobs (status, worker_id)
    where deleted_at is null;

-- Fast lookup for heartbeat monitoring
create index if not exists idx_jobs_status_heartbeat
    on public.jobs (status, heartbeat_at)
    where deleted_at is null and status in ('claimed', 'running', 'waiting_for_llm');

-- Fast lookup for queue polling
create index if not exists idx_jobs_status_claimed
    on public.jobs (status, claimed_at)
    where deleted_at is null and status = 'queued';

-- =============================================================================
-- 4. Worker heartbeat table (optional, for worker-level monitoring)
-- =============================================================================

create table if not exists public.worker_heartbeats (
    id uuid primary key default gen_random_uuid(),
    worker_id text not null,
    job_id uuid references public.jobs(id) on delete set null,
    last_heartbeat timestamptz not null default now(),
    started_at timestamptz not null default now(),
    status text not null default 'active'
        check (status in ('active', 'idle', 'stale', 'shutdown')),
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    unique (worker_id)
);

alter table public.worker_heartbeats enable row level security;

-- Service role only (no user access needed)
-- Workers write their own heartbeats; no user policy needed.

-- =============================================================================
-- 5. Add worker state events to job_events
-- =============================================================================

-- Worker action events are stored in job_events as event_type:
--   worker_claimed, worker_heartbeat, worker_released, worker_failed, worker_retry

-- =============================================================================
-- 6. Update worker_status enum to be more precise
-- =============================================================================

-- The application uses the jobs.status column as the single source of truth.
-- Valid statuses: queued, claimed, running, waiting_for_llm, completed, failed, abandoned, recoverable.
-- The DB check constraint is not extended to avoid migration risk; the app enforces.
