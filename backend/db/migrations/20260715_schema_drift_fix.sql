-- =============================================================================
-- ThinkSync — Schema Drift Remediation (forward-only, additive)
-- =============================================================================
-- Date:        2026-07-15
-- Scope:       Resolve VERIFIED database schema drift between the backend
--              (models/services) and the production schema (db/schema.sql,
--              which is documented as the single source of truth).
-- Policy:      ADDITIVE ONLY. No column/table/constraint removal, no rename,
--              no data loss, no breaking change. Every statement uses
--              "IF NOT EXISTS" guards so the file is idempotent and safe to
--              re-run. The Supabase migrator applies this file in a single
--              transaction (atomic).
--
-- ROOT CAUSE (verified by repo-wide audit, NOT by git history):
--   1. jobs.execution_cursor  — code writes/reads it (resume_manager.py,
--      interactive_wait.py, agent_service.py) but the column does not exist
--      in db/schema.sql -> runtime: "column jobs.execution_cursor does not exist"
--   2. jobs.interaction_state — code writes/reads it (interactive_wait.py,
--      resume_manager.py, agent_service.py) but the column does not exist in
--      db/schema.sql -> runtime: "column jobs.interaction_state does not exist"
--   3. approval_requests.updated_at — code assigns request.updated_at and
--      persists the whole model via model_dump_json()/UPDATE
--      (services/approval_engine.py:297, :306), and the ApprovalRequest pydantic
--      model lacks the field -> runtime: "ApprovalRequest object has no field
--      updated_at". The column is also absent from db/schema.sql.
--
-- Column types derived from code:
--   * execution_cursor  : stored via ExecutionCursor.model_dump(mode="json") -> jsonb
--   * interaction_state : stored via JobInteractionState.model_dump(mode="json") -> jsonb
--   * updated_at        : datetime (timezone-aware); matches the timestamptz
--                         convention used by created_at/resolved_at on the same table.
-- All three are nullable (code guards with `if x is not None` / `row.get(...)`).
--
-- NOT touched: existing migrations, other tables, any data, RLS policies.
-- =============================================================================

-- -----------------------------------------------------------------------------
-- 1. jobs: add the two resume/interaction state columns the backend uses.
-- -----------------------------------------------------------------------------
alter table public.jobs
    add column if not exists execution_cursor  jsonb,
    add column if not exists interaction_state jsonb;

-- -----------------------------------------------------------------------------
-- 2. approval_requests: add updated_at (written by ApprovalEngine._persist).
-- -----------------------------------------------------------------------------
alter table public.approval_requests
    add column if not exists updated_at timestamptz default now();
