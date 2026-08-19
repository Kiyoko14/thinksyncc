-- =============================================================================
-- ThinkSync — GitHub Connection layer (forward-only, additive)
-- =============================================================================
-- Date:        2026-07-17
-- Scope:       Introduce the GitHub connection credential vault + link a
--              connection to a workspace so the backend can clone/pull/push.
-- Policy:      ADDITIVE ONLY (same contract as 20260715_schema_drift_fix.sql).
--              Every statement is guarded with IF NOT EXISTS / IF ... IS NULL
--              so the file is idempotent and safe to re-run inside a single
--              Supabase transaction.
--
-- SECURITY NOTES:
--   * github_connections.ssh_private_key is ENCRYPTED AT REST by the backend
--     (core/crypto.encrypt_secret -> "enc:v1:..."). The application never
--     stores a plaintext private key.
--   * RLS is enabled and scoped to auth.uid() = user_id, exactly like the
--     existing servers/workspaces tables, so a connection cannot be read or
--     written cross-tenant.
--   * The private key column is selected only server-side (service role); the
--     router never serializes it (GitHubConnectionResponse omits it).
-- =============================================================================

-- -----------------------------------------------------------------------------
-- 1. github_connections: the credential vault entry (SSH only for now).
-- -----------------------------------------------------------------------------
create table if not exists public.github_connections (
    id uuid default gen_random_uuid() not null,
    user_id uuid not null,
    name text not null,
    auth_method text not null default 'ssh',
    host text not null default 'github.com',
    ssh_public_key text,
    ssh_private_key text,           -- ENCRYPTED AT REST ("enc:v1:..."). Never returned to client.
    ssh_key_type text,              -- 'ed25519' | 'rsa' | 'ecdsa' | 'unknown'
    created_at timestamp with time zone default now() not null,
    updated_at timestamp with time zone default now() not null
);

alter table public.github_connections
    add column if not exists id uuid;
alter table public.github_connections
    add column if not exists user_id uuid;
alter table public.github_connections
    add column if not exists name text;
alter table public.github_connections
    add column if not exists auth_method text;
alter table public.github_connections
    add column if not exists host text;
alter table public.github_connections
    add column if not exists ssh_public_key text;
alter table public.github_connections
    add column if not exists ssh_private_key text;
alter table public.github_connections
    add column if not exists ssh_key_type text;
alter table public.github_connections
    add column if not exists created_at timestamp with time zone;
alter table public.github_connections
    add column if not exists updated_at timestamp with time zone;

-- Primary key (guarded: only add if the constraint does not exist).
do $$
begin
    if not exists (
        select 1 from pg_constraint
        where conname = 'github_connections_pkey'
          and conrelid = 'public.github_connections'::regclass
    ) then
        alter table public.github_connections
            add constraint github_connections_pkey primary key (id);
    end if;
end $$;

-- Foreign key to auth.users (mirrors servers/workspaces convention).
do $$
begin
    if not exists (
        select 1 from pg_constraint
        where conname = 'github_connections_user_id_fkey'
          and conrelid = 'public.github_connections'::regclass
    ) then
        alter table public.github_connections
            add constraint github_connections_user_id_fkey
            foreign key (user_id) references auth.users (id);
    end if;
end $$;

-- Unique (user_id, name) so a user cannot create two connections with the
-- same display name.
do $$
begin
    if not exists (
        select 1 from pg_constraint
        where conname = 'github_connections_user_name_uniq'
          and conrelid = 'public.github_connections'::regclass
    ) then
        alter table public.github_connections
            add constraint github_connections_user_name_uniq
            unique (user_id, name);
    end if;
end $$;

-- -----------------------------------------------------------------------------
-- 2. workspaces: optional link to a GitHub connection (for clone on create).
-- -----------------------------------------------------------------------------
alter table public.workspaces
    add column if not exists github_connection_id uuid;

do $$
begin
    if not exists (
        select 1 from pg_constraint
        where conname = 'workspaces_github_connection_id_fkey'
          and conrelid = 'public.workspaces'::regclass
    ) then
        alter table public.workspaces
            add constraint workspaces_github_connection_id_fkey
            foreign key (github_connection_id)
            references public.github_connections (id);
    end if;
end $$;

-- Index for fast lookups by user.
create index if not exists idx_github_connections_user_id
    on public.github_connections (user_id);

-- -----------------------------------------------------------------------------
-- 3. Row Level Security (mirror servers/workspaces policy exactly).
-- -----------------------------------------------------------------------------
alter table public.github_connections enable row level security;

drop policy if exists "github_connections_owner" on public.github_connections;
create policy "github_connections_owner"
    on public.github_connections
    for all
    using (auth.uid() = user_id)
    with check (auth.uid() = user_id);

-- workspaces RLS already exists; no change needed, the new FK column is just
-- an attribute and is covered by the existing (auth.uid() = user_id) policy.
