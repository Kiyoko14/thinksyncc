-- ThinkSync MVP schema
-- Single source of truth: backend/db/schema.sql

create extension if not exists "uuid-ossp";
create extension if not exists "pgcrypto";

create table if not exists public.servers (
    id uuid primary key default gen_random_uuid(),
    user_id uuid not null references auth.users(id) on delete cascade,
    name text not null,
    host text not null,
    ssh_user text not null,
    ssh_port integer not null default 22 check (ssh_port between 1 and 65535),
    ssh_auth_method text not null check (ssh_auth_method in ('private_key', 'password')),
    ssh_key text,
    ssh_password text,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

create table if not exists public.workspaces (
    id uuid primary key default gen_random_uuid(),
    user_id uuid not null references auth.users(id) on delete cascade,
    server_id uuid not null references public.servers(id) on delete cascade,
    name text not null,
    path text not null,
    slug text not null,
    domain text not null,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

create table if not exists public.chats (
    id uuid primary key default gen_random_uuid(),
    server_id uuid not null references public.servers(id) on delete cascade,
    user_id uuid not null references auth.users(id) on delete cascade,
    name text not null,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

create table if not exists public.messages (
    id uuid primary key default gen_random_uuid(),
    chat_id uuid not null references public.chats(id) on delete cascade,
    role text not null check (role in ('user', 'assistant', 'system')),
    content text not null,
    created_at timestamptz not null default now()
);

create table if not exists public.chat_messages (
    id uuid primary key default gen_random_uuid(),
    workspace_id uuid not null references public.workspaces(id) on delete cascade,
    user_id uuid not null references auth.users(id) on delete cascade,
    role text not null check (role in ('user', 'assistant', 'system')),
    content text not null,
    created_at timestamptz not null default now()
);

create table if not exists public.jobs (
    id uuid primary key default gen_random_uuid(),
    user_id uuid not null references auth.users(id) on delete cascade,
    workspace_id uuid references public.workspaces(id) on delete cascade,
    server_id uuid not null references public.servers(id) on delete cascade,
    objective text not null,
    status text not null default 'queued'
        check (status in ('queued', 'running', 'waiting_for_llm', 'completed', 'failed')),
    allow_write boolean not null default false,
    dry_run boolean not null default false,
    task_mode text not null default 'complex'
        check (task_mode in ('simple', 'complex')),
    plan jsonb not null default '[]'::jsonb,
    steps jsonb not null default '[]'::jsonb,
    decisions jsonb not null default '[]'::jsonb,
    summary text,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

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

create table if not exists public.tasks (
    id uuid primary key default gen_random_uuid(),
    chat_id uuid not null references public.chats(id) on delete cascade,
    user_id uuid not null references auth.users(id) on delete cascade,
    state text not null default 'pending',
    created_at timestamptz not null default now()
);

create index if not exists idx_servers_user_id on public.servers (user_id);
create index if not exists idx_workspaces_user_id on public.workspaces (user_id);
create index if not exists idx_workspaces_server_id on public.workspaces (server_id);
create index if not exists idx_workspaces_server_slug on public.workspaces (server_id, slug);
create unique index if not exists idx_workspaces_server_slug_unique on public.workspaces (server_id, slug);
create unique index if not exists idx_workspaces_domain_unique on public.workspaces (domain);
create index if not exists idx_chats_user_id on public.chats (user_id);
create index if not exists idx_chats_server_id on public.chats (server_id);
create index if not exists idx_messages_chat_id on public.messages (chat_id);
create index if not exists idx_chat_messages_workspace_id on public.chat_messages (workspace_id, created_at);
create index if not exists idx_chat_messages_user_id on public.chat_messages (user_id, created_at);
create index if not exists idx_tasks_user_id on public.tasks (user_id);
create index if not exists idx_tasks_chat_id on public.tasks (chat_id);
create index if not exists idx_jobs_user_id on public.jobs (user_id);
create index if not exists idx_jobs_workspace_id on public.jobs (workspace_id, created_at desc);
create index if not exists idx_jobs_server_id on public.jobs (server_id);
create index if not exists idx_jobs_status on public.jobs (status);
create unique index if not exists idx_workspace_files_workspace_path_unique on public.workspace_files (workspace_id, path);
create index if not exists idx_workspace_files_workspace_id on public.workspace_files (workspace_id, updated_at desc);
create index if not exists idx_agent_context_logs_workspace_id on public.agent_context_logs (workspace_id, timestamp desc);

create or replace function public.set_updated_at()
returns trigger
language plpgsql
as $$
begin
    new.updated_at = now();
    return new;
end;
$$;

drop trigger if exists trg_servers_set_updated_at on public.servers;
create trigger trg_servers_set_updated_at
before update on public.servers
for each row
execute function public.set_updated_at();

drop trigger if exists trg_chats_set_updated_at on public.chats;
create trigger trg_chats_set_updated_at
before update on public.chats
for each row
execute function public.set_updated_at();

drop trigger if exists trg_workspaces_set_updated_at on public.workspaces;
create trigger trg_workspaces_set_updated_at
before update on public.workspaces
for each row
execute function public.set_updated_at();

drop trigger if exists trg_workspace_files_set_updated_at on public.workspace_files;
create trigger trg_workspace_files_set_updated_at
before update on public.workspace_files
for each row
execute function public.set_updated_at();

alter table public.servers enable row level security;
alter table public.workspaces enable row level security;
alter table public.chats enable row level security;
alter table public.messages enable row level security;
alter table public.chat_messages enable row level security;
alter table public.tasks enable row level security;
alter table public.jobs enable row level security;
alter table public.workspace_files enable row level security;
alter table public.agent_context_logs enable row level security;

drop policy if exists "Users manage own servers" on public.servers;
create policy "Users manage own servers"
on public.servers
for all
using (auth.uid() = user_id)
with check (auth.uid() = user_id);

drop policy if exists "Users manage own workspaces" on public.workspaces;
create policy "Users manage own workspaces"
on public.workspaces
for all
using (auth.uid() = user_id)
with check (auth.uid() = user_id);

drop policy if exists "Users manage own chats" on public.chats;
create policy "Users manage own chats"
on public.chats
for all
using (auth.uid() = user_id)
with check (auth.uid() = user_id);

drop policy if exists "Users manage own messages" on public.messages;
create policy "Users manage own messages"
on public.messages
for all
using (
    exists (
        select 1
        from public.chats c
        where c.id = messages.chat_id
          and c.user_id = auth.uid()
    )
)
with check (
    exists (
        select 1
        from public.chats c
        where c.id = messages.chat_id
          and c.user_id = auth.uid()
    )
);

drop policy if exists "Users manage own workspace chat messages" on public.chat_messages;
create policy "Users manage own workspace chat messages"
on public.chat_messages
for all
using (auth.uid() = user_id)
with check (auth.uid() = user_id);

drop policy if exists "Users manage own tasks" on public.tasks;
create policy "Users manage own tasks"
on public.tasks
for all
using (auth.uid() = user_id)
with check (auth.uid() = user_id);

drop policy if exists "Users manage own jobs" on public.jobs;
create policy "Users manage own jobs"
on public.jobs
for all
using (auth.uid() = user_id)
with check (auth.uid() = user_id);

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
