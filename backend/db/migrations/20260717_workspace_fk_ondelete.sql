-- =============================================================================
-- ThinkSync — Workspace Lifecycle (Part 4): FK ON DELETE SET NULL
-- =============================================================================
-- Date:   2026-07-17
-- Scope:  Make workspaces.github_connection_id survive a connection deletion by
--         setting it to NULL (workspace reverts to a ThinkSync workspace)
--         instead of leaving a dangling reference or blocking the delete.
-- Policy: ADDITIVE + idempotent. Drop-and-recreate the FK inside a guarded
--         DO block so re-running is safe. No data change.
--
-- WHY: the original FK (20260717_github_connection.sql) has no ON DELETE action
-- (defaults to NO ACTION), which can block connection cleanup or dangle. The
-- lifecycle orchestrator's DISCONNECT/DELETE flows rely on SET NULL semantics.
-- =============================================================================

do $$
begin
    -- Drop the existing FK if present (any ON DELETE variant), then recreate
    -- it with ON DELETE SET NULL.
    if exists (
        select 1 from pg_constraint
        where conname = 'workspaces_github_connection_id_fkey'
          and conrelid = 'public.workspaces'::regclass
    ) then
        alter table public.workspaces
            drop constraint workspaces_github_connection_id_fkey;
    end if;

    alter table public.workspaces
        add constraint workspaces_github_connection_id_fkey
        foreign key (github_connection_id)
        references public.github_connections (id)
        on delete set null;
end $$;
