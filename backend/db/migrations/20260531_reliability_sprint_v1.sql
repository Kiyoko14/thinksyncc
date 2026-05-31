-- ThinkSync Reliability Sprint v1: execution metadata separation
-- Adds dedicated tables for steps, decisions, retries, and execution details
-- to support durable audit trail and reconstruction.

-- =============================================================================
-- 1. job_steps: durable step execution records
-- =============================================================================

create table if not exists public.job_steps (
    id uuid primary key default gen_random_uuid(),
    job_id uuid not null references public.jobs(id) on delete cascade,
    step_number int not null,
    tool text not null,
    args jsonb not null default '{}'::jsonb,
    command text,
    command_type text,
    stdout text,
    stderr text,
    exit_code int,
    duration_ms int,
    success boolean not null default false,
    validation_passed boolean not null default false,
    status text,
    agent_reasoning text,
    executed_at timestamptz not null default now(),
    created_at timestamptz not null default now()
);

create index if not exists idx_job_steps_job_id on public.job_steps (job_id, step_number);

create index if not exists idx_job_steps_executed_at on public.job_steps (job_id, executed_at desc);

alter table public.job_steps enable row level security;

drop policy if exists "Users manage own job steps" on public.job_steps;
create policy "Users manage own job steps"
on public.job_steps
for all
using (
    exists (
        select 1 from public.jobs j
        where j.id = job_steps.job_id
          and j.user_id = auth.uid()
    )
)
with check (
    exists (
        select 1 from public.jobs j
        where j.id = job_steps.job_id
          and j.user_id = auth.uid()
    )
);

-- =============================================================================
-- 2. job_decisions: durable decision records
-- =============================================================================

create table if not exists public.job_decisions (
    id uuid primary key default gen_random_uuid(),
    job_id uuid not null references public.jobs(id) on delete cascade,
    step_number int,
    action text not null,
    reason text,
    summary_so_far text,
    modified_step jsonb,
    created_at timestamptz not null default now()
);

create index if not exists idx_job_decisions_job_id on public.job_decisions (job_id, created_at desc);

alter table public.job_decisions enable row level security;

drop policy if exists "Users manage own job decisions" on public.job_decisions;
create policy "Users manage own job decisions"
on public.job_decisions
for all
using (
    exists (
        select 1 from public.jobs j
        where j.id = job_decisions.job_id
          and j.user_id = auth.uid()
    )
)
with check (
    exists (
        select 1 from public.jobs j
        where j.id = job_decisions.job_id
          and j.user_id = auth.uid()
    )
);

-- =============================================================================
-- 3. job_retries: durable retry records
-- =============================================================================

create table if not exists public.job_retries (
    id uuid primary key default gen_random_uuid(),
    job_id uuid not null references public.jobs(id) on delete cascade,
    step_number int not null,
    attempt int not null,
    command text,
    command_type text,
    reason text,
    created_at timestamptz not null default now()
);

create index if not exists idx_job_retries_job_id on public.job_retries (job_id, created_at desc);

create index if not exists idx_job_retries_job_step on public.job_retries (job_id, step_number);

alter table public.job_retries enable row level security;

drop policy if exists "Users manage own job retries" on public.job_retries;
create policy "Users manage own job retries"
on public.job_retries
for all
using (
    exists (
        select 1 from public.jobs j
        where j.id = job_retries.job_id
          and j.user_id = auth.uid()
    )
)
with check (
    exists (
        select 1 from public.jobs j
        where j.id = job_retries.job_id
          and j.user_id = auth.uid()
    )
);

-- =============================================================================
-- 4. job_execution_details: errors, metadata, extra context
-- =============================================================================

create table if not exists public.job_execution_details (
    id uuid primary key default gen_random_uuid(),
    job_id uuid not null references public.jobs(id) on delete cascade,
    detail_type text not null check (detail_type in ('error','metadata','analysis','contract')),
    step_number int,
    payload jsonb not null default '{}'::jsonb,
    created_at timestamptz not null default now()
);

create index if not exists idx_job_execution_details_job_id on public.job_execution_details (job_id, detail_type, created_at desc);

alter table public.job_execution_details enable row level security;

drop policy if exists "Users manage own job execution details" on public.job_execution_details;
create policy "Users manage own job execution details"
on public.job_execution_details
for all
using (
    exists (
        select 1 from public.jobs j
        where j.id = job_execution_details.job_id
          and j.user_id = auth.uid()
    )
)
with check (
    exists (
        select 1 from public.jobs j
        where j.id = job_execution_details.job_id
          and j.user_id = auth.uid()
    )
);

-- =============================================================================
-- 5. jobs soft-delete support
-- =============================================================================

alter table public.jobs
    add column if not exists deleted_at timestamptz;

-- Update existing jobs listing queries to exclude deleted jobs
-- This is handled at application layer; the index is for fast lookup.

create index if not exists idx_jobs_deleted_at on public.jobs (deleted_at) where deleted_at is null;

-- =============================================================================
-- 6. Enrich job_events table with workspace_id
-- =============================================================================

-- workspace_id already exists on job_events; ensure index exists.

create index if not exists idx_job_events_workspace_id on public.job_events (workspace_id, created_at desc);

-- =============================================================================
-- 7. Add trace_id to job_events for correlation
-- =============================================================================

alter table public.job_events
    add column if not exists trace_id text;

create index if not exists idx_job_events_trace_id on public.job_events (trace_id);

-- =============================================================================
-- 8. Add recovery flag to jobs
-- =============================================================================

alter table public.jobs
    add column if not exists recoverable boolean not null default false;

alter table public.jobs
    add column if not exists recovery_reason text;

create index if not exists idx_jobs_recoverable on public.jobs (recoverable, status) where deleted_at is null;
