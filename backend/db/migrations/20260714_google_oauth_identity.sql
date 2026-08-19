-- Phase 0: ThinkSync custom identity model (Google OAuth migration)
-- ===========================================================================
-- Goal (per migration plan SPRINT_OAUTH_MIGRATION_PLAN.md):
--   * Replace auth.users as the application identity source with public.users.
--   * Supabase remains PostgreSQL only (no Supabase Auth).
--   * JWT subject becomes public.users.id.
--   * Store google_sub as a unique column (future OAuth providers reuse this table).
--   * Migrate every FK from auth.users -> public.users.
--   * Keep application-level authorization (service-role bypasses RLS; services
--     filter by user_id). Full RLS redesign is deferred to a later Security sprint.
--
-- Properties: additive + idempotent (IF NOT EXISTS / DROP ... IF EXISTS /
-- ON CONFLICT DO NOTHING). Safe to re-run.
-- ===========================================================================

begin;

-- 1. Canonical identity table. Provider-agnostic: a new OAuth provider only
--    needs its own *unique* column (e.g. github_sub) + a provider value, the
--    rest of the schema is reused unchanged.
create table if not exists public.users (
    id            uuid        primary key default gen_random_uuid(),
    email         text,                       -- nullable: legacy auth.users rows may lack it
    google_sub    text,                       -- Google subject; unique when present
    display_name  text,
    avatar_url    text,
    provider      text        not null default 'google',
    is_active     boolean     not null default true,
    last_login_at timestamptz,
    created_at    timestamptz not null default now(),
    updated_at    timestamptz not null default now()
);

-- Only one ThinkSync user per Google subject; only one per verified email.
-- Partial indexes allow multiple NULLs (legacy migrated rows) without collision.
create unique index if not exists idx_users_google_sub
    on public.users (google_sub) where google_sub is not null;
create unique index if not exists idx_users_email
    on public.users (email) where email is not null;

-- 2. Preserve existing identities 1:1. We keep auth.users.id as public.users.id
--    so every existing FK (servers, workspaces, chats, ...) stays valid. All
--    rows are copied (email may be null for legacy accounts).
insert into public.users (id, email, provider, created_at, last_login_at)
select id, email, 'google', created_at, last_sign_in_at
from auth.users
on conflict (id) do nothing;

-- 3. Remap foreign keys from auth.users -> public.users.
--    Postgres auto-names FKs <table>_<column>_fkey; the DROP IF EXISTS is a
--    no-op if the name differs. Verify with `\d <table>` on the target DB.
alter table public.servers       drop constraint if exists servers_user_id_fkey;
alter table public.servers       add constraint servers_user_id_fkey
    foreign key (user_id) references public.users(id) on delete cascade;

alter table public.workspaces    drop constraint if exists workspaces_user_id_fkey;
alter table public.workspaces    add constraint workspaces_user_id_fkey
    foreign key (user_id) references public.users(id) on delete cascade;

alter table public.chats         drop constraint if exists chats_user_id_fkey;
alter table public.chats         add constraint chats_user_id_fkey
    foreign key (user_id) references public.users(id) on delete cascade;

alter table public.chat_messages drop constraint if exists chat_messages_user_id_fkey;
alter table public.chat_messages add constraint chat_messages_user_id_fkey
    foreign key (user_id) references public.users(id) on delete cascade;

alter table public.jobs          drop constraint if exists jobs_user_id_fkey;
alter table public.jobs          add constraint jobs_user_id_fkey
    foreign key (user_id) references public.users(id) on delete cascade;

alter table public.tasks         drop constraint if exists tasks_user_id_fkey;
alter table public.tasks         add constraint tasks_user_id_fkey
    foreign key (user_id) references public.users(id) on delete cascade;

-- 4. Remove RLS policies that depend on auth.users / auth.uid().
--    Authorization stays application-level (service-role bypasses RLS; every
--    service scopes queries by user_id). Replacing these with a real RLS model
--    on public.users is explicitly deferred to a later Security/Hardening sprint.
drop policy if exists "Users manage own servers"              on public.servers;
drop policy if exists "Users manage own workspaces"          on public.workspaces;
drop policy if exists "Users manage own chats"               on public.chats;
drop policy if exists "Users manage own messages"            on public.messages;
drop policy if exists "Users manage own workspace chat messages" on public.chat_messages;
drop policy if exists "Users manage own tasks"               on public.tasks;
drop policy if exists "Users manage own jobs"                on public.jobs;
drop policy if exists "Users manage own job state transitions" on public.job_state_transitions;
drop policy if exists "Users manage own job events"          on public.job_events;
drop policy if exists "Users manage own job steps"           on public.job_steps;
drop policy if exists "Users manage own job decisions"       on public.job_decisions;
drop policy if exists "Users manage own job retries"         on public.job_retries;
drop policy if exists "Users manage own job execution details" on public.job_execution_details;
drop policy if exists "Users manage own workspace files"     on public.workspace_files;
drop policy if exists "Users manage own agent context logs"  on public.agent_context_logs;

commit;
