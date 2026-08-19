-- =============================================================================
-- ThinkSync — GitHub App Production Hardening · Part 7 (Observability)
-- =============================================================================
-- Date:        2026-07-17
-- Scope:       Extend the SINGLE existing github_audit_log table with the
--              structured observability fields required by Part 7. No new
--              audit table is created (no parallel subsystem). No breaking
--              change: all new columns are nullable (add column if not exists).
-- Policy:      ADDITIVE ONLY + idempotent (IF NOT EXISTS). Safe to re-run.
-- =============================================================================

-- Extended structured-audit columns (Part 7 minimal field set).
alter table public.github_audit_log
    add column if not exists workspace_id uuid;
alter table public.github_audit_log
    add column if not exists github_connection_id uuid;
alter table public.github_audit_log
    add column if not exists server_id uuid;
alter table public.github_audit_log
    add column if not exists request_id text;
alter table public.github_audit_log
    add column if not exists correlation_id text;
alter table public.github_audit_log
    add column if not exists step_name text;
alter table public.github_audit_log
    add column if not exists duration_ms integer;
alter table public.github_audit_log
    add column if not exists status text;

-- Correlation / join indices (additive).
create index if not exists idx_github_audit_log_correlation
    on public.github_audit_log (correlation_id);
create index if not exists idx_github_audit_log_workspace
    on public.github_audit_log (workspace_id);
create index if not exists idx_github_audit_log_connection
    on public.github_audit_log (github_connection_id);
create index if not exists idx_github_audit_log_request
    on public.github_audit_log (request_id);
create index if not exists idx_github_audit_log_step
    on public.github_audit_log (step_name);
