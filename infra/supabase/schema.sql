-- ThinkSync v2 — Supabase schema
-- Run this in the Supabase SQL editor to set up the MVP database.

-- ── Extensions ───────────────────────────────────────────────────────────────
create extension if not exists "uuid-ossp";

-- =============================================================================
-- ThinkSync custom identity model (Google OAuth migration, 2026-07-14)
-- Canonical identity table. Supabase Auth is removed; Supabase is PostgreSQL
-- only. JWT subject = public.users.id. Future OAuth providers reuse this table.
-- NOTE: this is a STALE v2 copy of backend/db/schema.sql; the canonical source
-- is backend/db/schema.sql. Regenerate from there after schema changes.
-- =============================================================================

create table if not exists public.users (
    id            uuid        primary key default uuid_generate_v4(),
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


-- ── servers ──────────────────────────────────────────────────────────────────
create table if not exists public.servers (
    id               uuid        primary key default uuid_generate_v4(),
    user_id          uuid        not null references public.users(id) on delete cascade,
    name             varchar(100) not null,
    host             varchar(255) not null,
    ssh_user         varchar(100) not null,
    ssh_port         integer     not null default 22
                                 check (ssh_port between 1 and 65535),
    ssh_auth_method  varchar(20) not null
                                 check (ssh_auth_method in ('password', 'key')),
    ssh_key          text,
    ssh_password     text,
    created_at       timestamptz not null default now()
);

create table if not exists public.workspaces (
    id          uuid         primary key default uuid_generate_v4(),
    user_id     uuid         not null references public.users(id) on delete cascade,
    server_id   uuid         not null references public.servers(id) on delete cascade,
    name        varchar(150) not null,
    path        text         not null,
    created_at  timestamptz  not null default now()
);


    user_id     uuid        not null references public.users(id) on delete cascade,



create table if not exists public.chat_messages (
    id           uuid        primary key default uuid_generate_v4(),
    workspace_id uuid        not null references public.workspaces(id) on delete cascade,
    user_id      uuid        not null references public.users(id) on delete cascade,
    role         varchar(20) not null check (role in ('user', 'assistant', 'system')),
    content      text        not null,
    created_at   timestamptz not null default now()
);

create table if not exists public.jobs (
    id           uuid         primary key default uuid_generate_v4(),
    user_id      uuid         not null references public.users(id) on delete cascade,
    workspace_id uuid         references public.workspaces(id) on delete cascade,
    server_id    uuid         not null references public.servers(id) on delete cascade,
    objective    text         not null,
    status       text         not null default 'queued'
        check (status in ('queued', 'running', 'waiting_for_llm', 'completed', 'failed')),
    allow_write  boolean      not null default false,
    dry_run      boolean      not null default false,
    task_mode    text         not null default 'complex'
        check (task_mode in ('simple', 'complex')),
    plan         jsonb        not null default '[]'::jsonb,
    steps        jsonb        not null default '[]'::jsonb,
    decisions    jsonb        not null default '[]'::jsonb,
    summary      text,
    created_at   timestamptz  not null default now(),
    updated_at   timestamptz  not null default now()
);

create table if not exists public.workspace_files (
    id            uuid         primary key default uuid_generate_v4(),
    workspace_id  uuid         not null references public.workspaces(id) on delete cascade,
    path          text         not null,
    size          bigint       not null default 0,
    last_modified timestamptz,
    language      text         not null default 'unknown',
    created_at    timestamptz  not null default now(),
    updated_at    timestamptz  not null default now()
);

create table if not exists public.agent_context_logs (
    id             uuid         primary key default uuid_generate_v4(),
    workspace_id   uuid         not null references public.workspaces(id) on delete cascade,
    task           text         not null,
    selected_files jsonb        not null default '[]'::jsonb,
    snippet_preview text,
    source         text         not null default 'fresh',
    timestamp      timestamptz  not null default now()
);





create unique index if not exists idx_workspace_files_workspace_path_unique
    on public.workspace_files (workspace_id, path);
create index if not exists idx_workspace_files_workspace_id
    on public.workspace_files (workspace_id, updated_at desc);
create index if not exists idx_agent_context_logs_workspace_id
    on public.agent_context_logs (workspace_id, timestamp desc);

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
