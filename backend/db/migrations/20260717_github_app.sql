-- ============================================================================
-- 20260717_github_app.sql — GitHub App (OAuth) integration, Phase 1
-- ============================================================================
-- Production-ready GitHub App backend. Design notes:
--   * The GitHub App PRIVATE KEY (PEM) is NEVER stored here. It lives in
--     configuration (env) only and is used in-process to mint short-lived
--     installation tokens.
--   * Installations store METADATA ONLY (account id/login/type, granted
--     permissions, repo count) — no token, no PEM.
--   * App-based workspaces reuse the existing `github_connections` table with
--     auth_method = 'app' (no SSH keys), so the workspace discriminator
--     (github_connection_id IS NULL -> ThinkSync workspace) keeps working with
--     ZERO changes to the existing schema/columns.
-- ============================================================================

-- 1. github_app_installations: installation metadata vault (no secrets).
create table if not exists public.github_app_installations (
    id text not null,
    user_id uuid not null,
    github_account_id text not null,
    github_account_login text not null,
    github_account_type text not null default 'User',
    permissions jsonb not null default '{}'::jsonb,
    repositories_count integer not null default 0,
    created_at timestamp with time zone default now() not null,
    updated_at timestamp with time zone default now() not null,
    constraint github_app_installations_pkey primary key (id),
    constraint github_app_installations_user_id_fkey
        foreign key (user_id) references public.users (id) on delete cascade
);

create index if not exists idx_github_app_installations_user_id
    on public.github_app_installations (user_id);

-- Row-level security: each user only sees their own installations.
alter table public.github_app_installations enable row level security;

drop policy if exists "github_app_installations_owner" on public.github_app_installations;
create policy "github_app_installations_owner"
    on public.github_app_installations
    for all
    using (auth.uid() = user_id)
    with check (auth.uid() = user_id);

-- 2. github_connections.auth_method now accepts 'app' (in addition to 'ssh').
--    Existing column definition has no CHECK constraint, so no migration of
--    constraints is required; we simply document the new accepted value.
--    (The application enforces the allowed set via GitHubAuthMethod validation.)
comment on column public.github_connections.auth_method is
    'Authentication method for the GitHub connection: ssh | app';
