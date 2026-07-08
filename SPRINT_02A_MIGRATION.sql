-- Sprint 2A — ProjectSpecification persistence
-- Run this in Supabase / Postgres before deploying.

CREATE TABLE IF NOT EXISTS project_specifications (
    id            uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    conversation_id text        NOT NULL UNIQUE,
    user_id        text        NOT NULL DEFAULT '',
    spec_json      jsonb       NOT NULL,
    created_at     timestamptz NOT NULL DEFAULT now(),
    updated_at     timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_project_specs_conversation
    ON project_specifications(conversation_id);

-- Also add specification column to jobs table (if not exists)
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS specification jsonb;