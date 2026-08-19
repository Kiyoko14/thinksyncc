create table if not exists public.workspace_files (
    id uuid primary key default gen_random_uuid(),
    workspace_id uuid not null references public.workspaces(id) on delete cascade,
    path text not null,
    size bigint not null default 0,
    last_modified timestamptz,
    language text not null default 'unknown',
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

create table if not exists public.agent_context_logs (
    id uuid primary key default gen_random_uuid(),
    workspace_id uuid not null references public.workspaces(id) on delete cascade,
    task text not null,
    selected_files jsonb not null default '[]'::jsonb,
    snippet_preview text,
    source text not null default 'fresh',
    timestamp timestamptz not null default now()
);

create unique index if not exists idx_workspace_files_workspace_path_unique
    on public.workspace_files (workspace_id, path);
create index if not exists idx_workspace_files_workspace_id
    on public.workspace_files (workspace_id, updated_at desc);
create index if not exists idx_agent_context_logs_workspace_id
    on public.agent_context_logs (workspace_id, timestamp desc);

drop trigger if exists trg_workspace_files_set_updated_at on public.workspace_files;
create trigger trg_workspace_files_set_updated_at
before update on public.workspace_files
for each row
execute function public.set_updated_at();

alter table public.workspace_files enable row level security;
alter table public.agent_context_logs enable row level security;

drop policy if exists "Users manage own workspace files" on public.workspace_files;
create policy "Users manage own workspace files"
on public.workspace_files
for all
using (
    exists (
        select 1
        from public.workspaces w
        where w.id = workspace_files.workspace_id
          and w.user_id = auth.uid()
    )
)
with check (
    exists (
        select 1
        from public.workspaces w
        where w.id = workspace_files.workspace_id
          and w.user_id = auth.uid()
    )
);

drop policy if exists "Users manage own agent context logs" on public.agent_context_logs;
create policy "Users manage own agent context logs"
on public.agent_context_logs
for all
using (
    exists (
        select 1
        from public.workspaces w
        where w.id = agent_context_logs.workspace_id
          and w.user_id = auth.uid()
    )
)
with check (
    exists (
        select 1
        from public.workspaces w
        where w.id = agent_context_logs.workspace_id
          and w.user_id = auth.uid()
    )
);
