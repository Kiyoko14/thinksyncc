-- ThinkSync v2: add intent routing fields to jobs table
-- Apply in Supabase SQL editor or your migration pipeline.

alter table public.jobs
    add column if not exists intent text not null default 'chat'
        check (intent in ('chat', 'code', 'server'));

create index if not exists idx_jobs_intent on public.jobs (intent, created_at desc);

