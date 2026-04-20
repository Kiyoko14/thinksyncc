-- ThinkSync v2: add structured agent outputs to jobs table
-- Apply in Supabase SQL editor or your migration pipeline.

alter table public.jobs
    add column if not exists errors jsonb not null default '[]'::jsonb,
    add column if not exists retries jsonb not null default '[]'::jsonb;

