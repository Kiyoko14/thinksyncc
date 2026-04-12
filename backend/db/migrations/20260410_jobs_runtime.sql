alter table public.jobs
    add column if not exists workspace_id uuid references public.workspaces(id) on delete cascade,
    add column if not exists allow_write boolean not null default false,
    add column if not exists dry_run boolean not null default false,
    add column if not exists task_mode text not null default 'complex'
        check (task_mode in ('simple', 'complex')),
    add column if not exists plan jsonb not null default '[]'::jsonb,
    add column if not exists decisions jsonb not null default '[]'::jsonb;

create index if not exists idx_jobs_workspace_id on public.jobs (workspace_id, created_at desc);
