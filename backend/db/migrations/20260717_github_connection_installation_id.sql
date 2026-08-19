-- =============================================================================
-- ThinkSync — GitHub connection: installation_id column (forward-only, additive)
-- =============================================================================
-- Date:        2026-07-17
-- Scope:       Give `github_connections` a first-class `installation_id` column
--              so an App-based connection (auth_method='app') carries its GitHub
--              App installation id in a dedicated field instead of encoding it
--              inside the display `name` (which was a bridge/scaffold and is now
--              removed). SSH connections leave this column NULL.
-- Policy:      ADDITIVE ONLY + idempotent. Safe to re-run. No data migration of
--              existing rows required (SSH rows are unaffected; the column is
--              nullable). No constraints changed.
--
-- WHY:         The agent tools (github_pull / github_push) resolve the credential
--              provider purely from the connection row. For auth_method='app'
--              they need the installation id to mint a short-lived installation
--              token. Reading it from a structured `name` string was fragile;
--              a real column is the production-grade source of truth.
-- =============================================================================

alter table public.github_connections
    add column if not exists installation_id text;

comment on column public.github_connections.installation_id is
    'GitHub App installation id (only set when auth_method=''app''; NULL for ssh).';
