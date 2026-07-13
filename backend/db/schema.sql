-- ThinkSync MVP schema
-- Single source of truth: backend/db/schema.sql

create extension if not exists "uuid-ossp";
create extension if not exists "pgcrypto";


-- =============================================================================
-- ThinkSync custom identity model (Google OAuth migration, 2026-07-14)
-- Canonical identity table. Supabase Auth is removed; Supabase is PostgreSQL
-- only. JWT subject = public.users.id. Future OAuth providers (GitHub,
-- Microsoft, Telegram, Apple) reuse this table with a provider-specific unique
-- column (e.g. github_sub) instead of google_sub.
-- =============================================================================

create table if not exists public.users (
    id            uuid        primary key default gen_random_uuid(),
    email         text,
    google_sub    text,
    display_name  text,
    avatar_url    text,
    provider      text        not null default 'google',
    is_active     boolean     not null default true,
    last_login_at timestamptz,
    created_at    timestamptz not null default now(),
    updated_at    timestamptz not null default now()
);

create unique index if not exists idx_users_google_sub
    on public.users (google_sub) where google_sub is not null;
create unique index if not exists idx_users_email
    on public.users (email) where email is not null;


create table if not exists public.servers (
    id uuid primary key default gen_random_uuid(),
    user_id uuid not null references public.users(id) on delete cascade,
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
    user_id uuid not null references public.users(id) on delete cascade,
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
    user_id uuid not null references public.users(id) on delete cascade,
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
    user_id uuid not null references public.users(id) on delete cascade,
    role text not null check (role in ('user', 'assistant', 'system')),
    content text not null,
    created_at timestamptz not null default now()
);

create table if not exists public.jobs (
    id uuid primary key default gen_random_uuid(),
    user_id uuid not null references public.users(id) on delete cascade,
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
    errors jsonb not null default '[]'::jsonb,
    retries jsonb not null default '[]'::jsonb,
    summary text,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

create table if not exists public.job_state_transitions (
    id uuid primary key default gen_random_uuid(),
    job_id uuid not null references public.jobs(id) on delete cascade,
    from_status text check (from_status in ('queued', 'running', 'waiting_for_llm', 'completed', 'failed')),
    to_status text not null check (to_status in ('queued', 'running', 'waiting_for_llm', 'completed', 'failed')),
    step integer,
    tool text,
    trace_id text,
    reason text,
    created_at timestamptz not null default now()
);

create table if not exists public.job_events (
    id uuid primary key default gen_random_uuid(),
    job_id uuid not null references public.jobs(id) on delete cascade,
    workspace_id uuid references public.workspaces(id) on delete set null,
    sequence bigint not null default 0,
    event_type text not null,
    payload jsonb not null,
    trace_id text,
    created_at timestamptz not null default now()
);

-- =============================================================================
-- Execution metadata separation (Reliability Sprint v1)
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

create table if not exists public.job_execution_details (
    id uuid primary key default gen_random_uuid(),
    job_id uuid not null references public.jobs(id) on delete cascade,
    detail_type text not null check (detail_type in ('error','metadata','analysis','contract')),
    step_number int,
    payload jsonb not null default '{}'::jsonb,
    created_at timestamptz not null default now()
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
    user_id uuid not null references public.users(id) on delete cascade,
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
create index if not exists idx_job_state_transitions_job_id on public.job_state_transitions (job_id, created_at desc);
create index if not exists idx_job_events_job_id on public.job_events (job_id, created_at desc);
create unique index if not exists idx_job_events_job_sequence on public.job_events (job_id, sequence);
create index if not exists idx_job_events_workspace_id on public.job_events (workspace_id, created_at desc);
create index if not exists idx_job_events_trace_id on public.job_events (trace_id);

create index if not exists idx_job_steps_job_id on public.job_steps (job_id, step_number);
create index if not exists idx_job_steps_executed_at on public.job_steps (job_id, executed_at desc);
create index if not exists idx_job_decisions_job_id on public.job_decisions (job_id, created_at desc);
create index if not exists idx_job_retries_job_id on public.job_retries (job_id, created_at desc);
create index if not exists idx_job_retries_job_step on public.job_retries (job_id, step_number);
create index if not exists idx_job_execution_details_job_id on public.job_execution_details (job_id, detail_type, created_at desc);
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





-- Reliability Sprint v1: new audit tables









-- Soft-delete and recovery support on jobs

alter table public.jobs
    add column if not exists deleted_at timestamptz,
    add column if not exists recoverable boolean not null default false,
    add column if not exists recovery_reason text;

create index if not exists idx_jobs_deleted_at on public.jobs (deleted_at) where deleted_at is null;
create index if not exists idx_jobs_recoverable on public.jobs (recoverable, status) where deleted_at is null;

-- =============================================================================
-- Row-Level Security
-- -----------------------------------------------------------------------------
-- During the Google OAuth migration RLS that depended on auth.users / auth.uid()
-- was removed. Authorization is currently enforced at the application layer: the
-- backend connects with the Supabase service-role key (which bypasses RLS) and
-- every service scopes queries by user_id (e.g. .eq("user_id", user_id)).
-- A real RLS model on public.users is scheduled for a later Security/Hardening
-- sprint (see migration notes) and intentionally NOT redesigned here.
-- =============================================================================
