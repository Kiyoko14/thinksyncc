-- Sprint 2C — Update project_specifications table for versioning
-- Run this in Supabase SQL Editor AFTER backing up data

-- 1. Add new columns if they don't exist
ALTER TABLE project_specifications
  ADD COLUMN IF NOT EXISTS spec_versions jsonb DEFAULT '[]'::jsonb,
  ADD COLUMN IF NOT EXISTS latest_version int DEFAULT 1;

-- 2. Migrate existing single-spec rows to versioned format
-- (Run this ONCE after the column is added)
UPDATE project_specifications
SET
  spec_versions = jsonb_build_array(
    jsonb_build_object(
      'version', 1,
      'spec_json', COALESCE(spec_json, '{}'::jsonb),
      'frozen_at', now(),
      'frozen_hash', '',
      'parent_version', NULL,
      'change_reason', 'Migrated from single-spec format',
      'changed_fields', '{}'::jsonb,
      'assumptions', '[]'::jsonb,
      'review_verdict', 'pass'
    )
  ),
  latest_version = 1
WHERE
  spec_versions IS NULL OR spec_versions = '[]'::jsonb;

-- 3. Backward-compat: keep spec_json column (used by older code reading single spec)
-- No drop — older reads still work.

-- 4. Add index on conversation_id (already should exist, but ensure)
CREATE UNIQUE INDEX IF NOT EXISTS idx_project_specs_conversation_id
  ON project_specifications(conversation_id);
