-- ThinkSync v2: jobs table
-- Steps are stored as a JSONB array on the job row — no extra joins needed.
-- Apply in Supabase SQL editor or your migration pipeline.

create table if not exists public.jobs (
    id           uuid primary key default gen_random_uuid(),
    user_id      uuid not null references auth.users(id) on delete cascade,
    server_id    uuid not null references public.servers(id) on delete cascade,
    objective    text not null,
    status       text not null default 'queued'
                     check (status in ('queued', 'running', 'waiting_for_llm', 'completed', 'failed')),
    steps        jsonb not null default '[]'::jsonb,
    summary      text,
    created_at   timestamptz not null default now(),
    updated_at   timestamptz not null default now()
);

create index if not exists idx_jobs_user_id    on public.jobs (user_id);
create index if not exists idx_jobs_server_id  on public.jobs (server_id);
create index if not exists idx_jobs_status     on public.jobs (status);
create index if not exists idx_jobs_created_at on public.jobs (created_at desc);

-- Auto-update updated_at on every row change.
drop trigger if exists trg_jobs_set_updated_at on public.jobs;
create trigger trg_jobs_set_updated_at
before update on public.jobs
for each row
execute function public.set_updated_at();

-- Row-level security: users can only see and modify their own jobs.
alter table public.jobs enable row level security;

drop policy if exists "Users manage own jobs" on public.jobs;
create policy "Users manage own jobs"
on public.jobs
for all
using (auth.uid() = user_id)
with check (auth.uid() = user_id);
