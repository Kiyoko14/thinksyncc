-- ThinkSync v2: durable job event and state transition persistence
-- Apply in Supabase SQL editor or your migration pipeline.

create table if not exists public.job_state_transitions (
    id uuid primary key default gen_random_uuid(),
    job_id uuid not null references public.jobs(id) on delete cascade,
    from_status text check (from_status in ('queued', 'running', 'waiting_for_llm', 'completed', 'failed')),
    to_status text not null check (to_status in ('queued', 'running', 'waiting_for_llm', 'completed', 'failed')),
    step integer,
    tool text,
    trace_id text,
    created_at timestamptz not null default now()
);

create table if not exists public.job_events (
    id uuid primary key default gen_random_uuid(),
    job_id uuid not null references public.jobs(id) on delete cascade,
    sequence bigint not null default 0,
    event_type text not null,
    payload jsonb not null,
    created_at timestamptz not null default now()
);

create index if not exists idx_job_state_transitions_job_id on public.job_state_transitions (job_id, created_at desc);
create index if not exists idx_job_events_job_id on public.job_events (job_id, created_at desc);
create unique index if not exists idx_job_events_job_sequence on public.job_events (job_id, sequence);

-- row-level security policies for job-level audit entities
alter table public.job_state_transitions enable row level security;
alter table public.job_events enable row level security;

drop policy if exists "Users manage own job state transitions" on public.job_state_transitions;
create policy "Users manage own job state transitions"
on public.job_state_transitions
for all
using (
    exists (
        select 1 from public.jobs j
        where j.id = job_state_transitions.job_id
          and j.user_id = auth.uid()
    )
)
with check (
    exists (
        select 1 from public.jobs j
        where j.id = job_state_transitions.job_id
          and j.user_id = auth.uid()
    )
);

-- allow users to retrieve their own job event history

drop policy if exists "Users manage own job events" on public.job_events;
create policy "Users manage own job events"
on public.job_events
for all
using (
    exists (
        select 1 from public.jobs j
        where j.id = job_events.job_id
          and j.user_id = auth.uid()
    )
)
with check (
    exists (
        select 1 from public.jobs j
        where j.id = job_events.job_id
          and j.user_id = auth.uid()
    )
);
