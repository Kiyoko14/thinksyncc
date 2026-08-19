-- =============================================================================
-- ThinkSync — GitHub App Production Hardening · Part 1 (Webhook Infrastructure)
-- =============================================================================
-- Date:        2026-07-17
-- Scope:       Webhook delivery ledger (replay protection), minimal audit log,
--              installation lifecycle status, and canonical repository identity
--              on github_connections (repo_id immutable + repo_full_name mutable).
-- Policy:      ADDITIVE ONLY + idempotent (IF NOT EXISTS / guarded DO blocks).
--              No breaking change. No data migration of existing rows required.
--
-- WHY (per approved Part 1 architecture decision):
--   * github_webhook_deliveries : each X-GitHub-Delivery UUID is recorded once;
--     a repeat delivery is detected and skipped (idempotent 200).
--   * github_audit_log          : minimal structured audit for webhook events.
--     Part 7 will EXTEND (not recreate) this table with more event types.
--   * github_app_installations.status : represents installation lifecycle
--     (active | suspended | deleted) so suspend/unsuspend/delete webhooks have
--     a home. installation.deleted is a SOFT delete (status='deleted').
--   * github_connections.repo_id (canonical, immutable) + repo_full_name
--     (mutable): repository.renamed / repository.deleted webhooks map by the
--     immutable repo_id; rename only updates repo_full_name.
-- =============================================================================

-- -----------------------------------------------------------------------------
-- 1. github_webhook_deliveries: replay-protection ledger.
-- -----------------------------------------------------------------------------
create table if not exists public.github_webhook_deliveries (
    delivery_id text not null,
    event_type text not null,
    action text,
    received_at timestamp with time zone default now() not null,
    constraint github_webhook_deliveries_pkey primary key (delivery_id)
);

create index if not exists idx_github_webhook_deliveries_received_at
    on public.github_webhook_deliveries (received_at desc);

-- Row-level security: this is a backend-only ledger (service role writes/reads).
-- Enable RLS with no permissive policy so only the service role key can touch it.
alter table public.github_webhook_deliveries enable row level security;

-- -----------------------------------------------------------------------------
-- 2. github_audit_log: minimal structured audit for GitHub App events.
--    Part 7 extends this; Part 1 only writes webhook-driven events.
-- -----------------------------------------------------------------------------
create table if not exists public.github_audit_log (
    id uuid default gen_random_uuid() not null,
    event_type text not null,
    installation_id text,
    user_id uuid,
    repo_id bigint,
    repo_full_name text,
    metadata jsonb not null default '{}'::jsonb,
    created_at timestamp with time zone default now() not null,
    constraint github_audit_log_pkey primary key (id)
);

create index if not exists idx_github_audit_log_installation
    on public.github_audit_log (installation_id, created_at desc);
create index if not exists idx_github_audit_log_event_type
    on public.github_audit_log (event_type, created_at desc);

alter table public.github_audit_log enable row level security;

-- -----------------------------------------------------------------------------
-- 3. github_app_installations.status: lifecycle state.
--    active (default) | suspended | deleted. installation.deleted is SOFT.
-- -----------------------------------------------------------------------------
alter table public.github_app_installations
    add column if not exists status text not null default 'active';

comment on column public.github_app_installations.status is
    'Installation lifecycle: active | suspended | deleted (deleted is a soft delete).';

-- -----------------------------------------------------------------------------
-- 4. github_connections: canonical repository identity.
--    repo_id      : GitHub numeric repository id — IMMUTABLE canonical key.
--    repo_full_name: owner/name — MUTABLE (updated on repository.renamed).
-- -----------------------------------------------------------------------------
alter table public.github_connections
    add column if not exists repo_id bigint;
alter table public.github_connections
    add column if not exists repo_full_name text;

comment on column public.github_connections.repo_id is
    'GitHub repository id: immutable canonical key for webhook mapping (rename/delete/transfer).';
comment on column public.github_connections.repo_full_name is
    'GitHub repository full name (owner/name): mutable; used for clone URL / remote / display.';

-- Lookup index for webhook mapping by the canonical repo_id.
create index if not exists idx_github_connections_repo_id
    on public.github_connections (repo_id);
