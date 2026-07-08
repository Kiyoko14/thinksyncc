-- Sprint 3: Interactive Approval & Human-in-the-Loop Orchestration
-- Run this migration in Supabase before deploying Sprint 3 changes.

-- -------------------------------------------------------------------------
-- 1. approval_requests table
-- -------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS approval_requests (
    approval_id        TEXT PRIMARY KEY,
    job_id             TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    conversation_id     TEXT NOT NULL,
    approval_type       TEXT NOT NULL,
    status              TEXT NOT NULL DEFAULT 'pending',
    title               TEXT DEFAULT '',
    description         TEXT DEFAULT '',
    risk_level          TEXT DEFAULT 'medium',
    affected_files      JSONB DEFAULT '[]',
    affected_commands   JSONB DEFAULT '[]',
    affected_assumptions JSONB DEFAULT '[]',
    context             JSONB DEFAULT '{}',
    created_at          TIMESTAMPTZ DEFAULT NOW(),
    resolved_at         TIMESTAMPTZ,
    resolved_by         TEXT DEFAULT '',
    decision            TEXT,
    reason              TEXT DEFAULT '',
    spec_version        INT,
    requirement_version INT
);

CREATE INDEX IF NOT EXISTS idx_approval_requests_job
    ON approval_requests(job_id);
CREATE INDEX IF NOT EXISTS idx_approval_requests_status
    ON approval_requests(status);

-- -------------------------------------------------------------------------
-- 2. approval_audit table
-- -------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS approval_audit (
    event_id          TEXT PRIMARY KEY,
    approval_id       TEXT NOT NULL REFERENCES approval_requests(approval_id) ON DELETE CASCADE,
    job_id            TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    conversation_id    TEXT NOT NULL,
    event_type         TEXT NOT NULL,
    timestamp          TIMESTAMPTZ DEFAULT NOW(),
    decision           TEXT,
    reason             TEXT DEFAULT '',
    "user"             TEXT DEFAULT '',
    metadata           JSONB DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_approval_audit_approval
    ON approval_audit(approval_id);
CREATE INDEX IF NOT EXISTS idx_approval_audit_job
    ON approval_audit(job_id);

-- -------------------------------------------------------------------------
-- 3. Extend jobs table with interaction columns
-- -------------------------------------------------------------------------

ALTER TABLE jobs
    ADD COLUMN IF NOT EXISTS interaction_state    JSONB DEFAULT '{}',
    ADD COLUMN IF NOT EXISTS execution_cursor    JSONB DEFAULT '{}',
    ADD COLUMN IF NOT EXISTS clarification_session JSONB DEFAULT '{}';

-- Extend jobs.status CHECK constraint to allow new Sprint 3 states
-- (Supabase/Postgres may need the constraint dropped and recreated)
ALTER TABLE jobs
    DROP CONSTRAINT IF EXISTS jobs_status_check;

ALTER TABLE jobs
    ADD CONSTRAINT jobs_status_check
    CHECK (
        status IN (
            'pending',
            'running',
            'waiting_for_user',
            'approved',
            'rejected',
            'resumed',
            'completed',
            'failed',
            'paused',
            'cancelled'
        )
    );

-- -------------------------------------------------------------------------
-- 4. Extend project_specifications with approval context
-- -------------------------------------------------------------------------

ALTER TABLE project_specifications
    ADD COLUMN IF NOT EXISTS approval_context JSONB DEFAULT '{}';

-- -------------------------------------------------------------------------
-- 5. Row-Level Security (RLS)
-- -------------------------------------------------------------------------

ALTER TABLE approval_requests ENABLE ROW LEVEL SECURITY;
ALTER TABLE approval_audit   ENABLE ROW LEVEL SECURITY;

-- Allow authenticated users to read/write their own project's approvals
-- (Adjust policies to match your Supabase auth setup)
DROP POLICY IF EXISTS approval_requests_user ON approval_requests;
CREATE POLICY approval_requests_user
    ON approval_requests
    FOR ALL
    USING (true)      -- TODO: restrict to owning conversation
    WITH CHECK (true);

DROP POLICY IF EXISTS approval_audit_user ON approval_audit;
CREATE POLICY approval_audit_user
    ON approval_audit
    FOR ALL
    USING (true)
    WITH CHECK (true);

-- -------------------------------------------------------------------------
-- 6. Verify
-- -------------------------------------------------------------------------

-- SELECT table_name FROM information_schema.tables
-- WHERE table_name IN ('approval_requests', 'approval_audit');
--
-- SELECT column_name, data_type
-- FROM information_schema.columns
-- WHERE table_name = 'jobs'
-- ORDER BY ordinal_position;
