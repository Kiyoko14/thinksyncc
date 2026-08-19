-- Audit table for Emergent E1 agent executions
-- Apply in Supabase SQL editor or migration pipeline.

create table if not exists public.agent_runs (
    id uuid primary key default gen_random_uuid(),
    user_id uuid not null,
    user_email text not null,
    server_id uuid not null,
    objective text not null,
    dry_run boolean not null default false,
    allow_write boolean not null default false,
    max_steps integer not null,
    plan jsonb not null,
    results jsonb not null,
    summary text not null,
    success boolean not null default false,
    created_at timestamptz not null default now()
);

create index if not exists idx_agent_runs_user_id on public.agent_runs(user_id);
create index if not exists idx_agent_runs_server_id on public.agent_runs(server_id);
create index if not exists idx_agent_runs_created_at on public.agent_runs(created_at desc);
