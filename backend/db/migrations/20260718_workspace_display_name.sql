-- =============================================================================
-- ThinkSync — Workspace Display Name (forward-only, additive)
-- =============================================================================
-- Date:        2026-07-18
-- Scope:       Add a dedicated `display_name` column to `workspaces` so the
--              user's original, human-readable workspace name is preserved
--              separately from the internal `name` (sanitized) and `slug`.
-- Policy:      ADDITIVE ONLY + idempotent. Safe to re-run. No data loss.
--
-- WHY:         Previously the workspace name provided by the user was
--              sanitized/slugified into `name` and `slug`, destroying the
--              original display name. The AI agent and end-user responses
--              must refer to the workspace by its real name (e.g.
--              "My Telegram Bot"), never by the internal slug.
-- =============================================================================

alter table public.workspaces
    add column if not exists display_name text;

comment on column public.workspaces.display_name is
    'Original user-provided workspace name (human-readable). Kept separate from the sanitized name/slug.';

-- Backfill: for any existing rows where display_name is null, use the
-- sanitized `name` as a fallback so no row is left without a display name.
update public.workspaces
    set display_name = name
    where display_name is null;
