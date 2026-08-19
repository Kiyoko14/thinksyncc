# DATABASE SCHEMA DRIFT — ROOT-CAUSE + FIX REPORT

**Rules honored:** no Git (no status/diff/log/blame); no existing migration modified; one new
forward-only migration; no destructive SQL; no Python business-logic change (only the minimal
`updated_at` field added to the `ApprovalRequest` model for schema/model parity, required to resolve
the Python-side error). SQL/types derived from code, not guessed.

---

## A. ROOT CAUSE SUMMARY

Three runtime errors were reported. All three are **real, verified schema drift** between the backend
(models/services) and the production schema (`db/schema.sql`, documented as the single source of truth):

1. `jobs.execution_cursor does not exist` — backend writes/reads `jobs.execution_cursor`
   (`resume_manager.py`, `interactive_wait.py`, `agent_service.py`) but the column is absent from
   `db/schema.sql`'s `jobs` table.
2. `jobs.interaction_state does not exist` — backend writes/reads `jobs.interaction_state`
   (`interactive_wait.py`, `resume_manager.py`, `agent_service.py`) but the column is absent from
   `db/schema.sql`'s `jobs` table.
3. `ApprovalRequest object has no field updated_at` — `services/approval_engine.py:297` assigns
   `request.updated_at = datetime.now(timezone.utc)` and persists the whole model via
   `model_dump_json()` + `.update(data)` (line 299/306). The `ApprovalRequest` Pydantic model
   (models/approval.py:134) and `db/schema.sql`'s `approval_requests` table BOTH lack `updated_at`,
   so the assignment raises `ValueError: object has no field 'updated_at'`.

Not drift (verified, excluded):
- `approval_state` — appears ONLY inside error-message `detail=` strings in `event_wait_engine.py`;
  it is **not** a `jobs` column.
- `resume_state`, `conversation_state`, `execution_metadata` — **zero** code references (task-suggested
  names that do not exist in the repository). Excluded as false leads.

## B. SCHEMA DRIFT TABLE

| Object | Repository (code/model) | Database (`db/schema.sql`) | Status |
|--------|-------------------------|----------------------------|--------|
| `jobs.execution_cursor` | written/read (resume_manager, interactive_wait, agent_service) | **MISSING** | DRIFT — add column |
| `jobs.interaction_state` | written/read (interactive_wait, resume_manager, agent_service) | **MISSING** | DRIFT — add column |
| `approval_requests.updated_at` | assigned (approval_engine.py:297) + persisted | **MISSING** (table + model) | DRIFT — add column + add model field |
| `approval_requests.resume_token` | table has it (mig 20260713); model does NOT declare it; never written/read via model | present in table | Benign one-directional gap — left as-is (see §I) |
| all other `jobs` columns | match `db/schema.sql` | present | OK |
| all other `approval_requests` columns | match model | present | OK |

## C. MIGRATION FILE NAME

`db/migrations/20260715_schema_drift_fix.sql` (new; forward-only; existing migrations untouched).

## D. COMPLETE SQL MIGRATION

```sql
-- =============================================================================
-- ThinkSync — Schema Drift Remediation (forward-only, additive)
-- =============================================================================
alter table public.jobs
    add column if not exists execution_cursor  jsonb,
    add column if not exists interaction_state jsonb;

alter table public.approval_requests
    add column if not exists updated_at timestamptz default now();
```

(The committed file includes full header comments documenting root cause, column-type justification,
and the additive/idempotent policy.)

## E. COLUMNS ADDED

- `public.jobs.execution_cursor`  — `jsonb` (nullable). Stored via `ExecutionCursor.model_dump(mode="json")`.
- `public.jobs.interaction_state` — `jsonb` (nullable). Stored via `JobInteractionState.model_dump(mode="json")`.
- `public.approval_requests.updated_at` — `timestamptz default now()` (nullable). Matches `created_at`/`resolved_at`
  convention on the same table.

All three use `ADD COLUMN IF NOT EXISTS` → idempotent and re-runnable.

## F. COLUMNS VERIFIED ALREADY EXISTING (no change)

`jobs`: id, user_id, workspace_id, server_id, objective, status, allow_write, dry_run, task_mode, plan,
steps, decisions, errors, retries, summary, created_at, updated_at, deleted_at, recoverable,
recovery_reason, intent, worker_id, claimed_at, heartbeat_at, completed_at, conversation_id,
conversation_session, cursor_version, spec (all present in `db/schema.sql`).

`approval_requests`: approval_id, job_id, conversation_id, approval_type, status, title, description,
risk_level, affected_files, affected_commands, affected_assumptions, context, created_at, resolved_at,
resolved_by, decision, reason, spec_version, requirement_version, request_version, resume_token
(all present; only `updated_at` was missing).

## G. MODEL PARITY VERIFICATION

- `ApprovalRequest` (models/approval.py) — added `updated_at: datetime | None = None` at line ~162.
  After the addition: every column the code uses (`updated_at`) is declared; `model_dump_json()` now
  includes `updated_at`; `ApprovalRequest(**row)` accepts a DB row containing `updated_at`.
- `updated_at` assign + dump + construct-from-row validated in a smoke test (passes).
- `jobs` columns are accessed via the Supabase client, not a Pydantic model, so no model change needed
  there — the migration alone closes that gap.
- `resume_token`: table has it (mig 20260713); model does NOT declare it and code never writes/reads it
  through the model. Left as-is deliberately (see §I) so `.update(data)` cannot clobber the column with NULL.

## H. RUNTIME ISSUES EXPECTED TO DISAPPEAR

After the migration is applied to production:
- `column jobs.execution_cursor does not exist` — resolved (column now exists).
- `column jobs.interaction_state does not exist` — resolved (column now exists).
- `ApprovalRequest object has no field updated_at` — resolved (model field added; assignment at
  approval_engine.py:297 now succeeds and persists).

## I. REMAINING UNRELATED ISSUES

- `approval_requests.resume_token`: present in DB (mig 20260713) but not declared on the `ApprovalRequest`
  model and unused via the model. Benign (no runtime error). Deliberately NOT added to the model: adding it
  with a `None` default would make `model_dump_json()` emit `"resume_token": null` and the `.update(data)` at
  approval_engine.py:306 would overwrite any stored value with NULL (data-loss risk). Out of scope of the
  three reported errors; left for a separate, explicit decision if the column is to be model-backed.
- Pre-existing, out-of-scope runtime issues from earlier investigations:
  - WebSocket send-after-close lifecycle in `routers/ws.py` (Issue 4).
  - Pre-existing test failures `test_endpoints.py` / `tests/test_google_oauth.py` (unrelated).

## J. VALIDATION

- **AST**: full repo audit located every `.table("jobs")`/`.table("approval_requests")` `.select()`/`.update()`/
  `.insert()` and diffed referenced columns against `db/schema.sql`. Confirmed `execution_cursor`,
  `interaction_state` missing from `jobs` and `updated_at` missing from `approval_requests`; confirmed
  `approval_state`/`resume_state`/`conversation_state`/`execution_metadata` are NOT real columns.
- **grep**: `.table("jobs")` + `execution_cursor`/`interaction_state`/`updated_at` cross-checked against AST —
  consistent. grep `updated_at` in `approval_engine.py` pinpoints line 297 (the assignment).
- **SQL consistency**: every added column name + type is justified by code
  (`jsonb` from `model_dump(mode="json")`; `timestamptz` matching sibling timestamp columns). No guessed columns.
- **Migration safety**: only 3 `ADD COLUMN IF NOT EXISTS` statements; no DROP/DELETE/RENAME/TRUNCATE;
  idempotent; additive; no data loss; applied atomically by the Supabase migrator.
- **py_compile**: `models/approval.py` compiles.
- **Import smoke + model behavior**: `ApprovalRequest` instantiates, `updated_at` assigns, serializes, and
  constructs from a DB row — all pass.
- **Regression tests**: targeted suite `211 passed, 1 skipped, 0 failures` (identical to pre-fix baseline).

## K. NOT VERIFIED

- Whether the LIVE production database also already contains any of these columns (e.g. added manually out of
  band). The migration uses `IF NOT EXISTS`, so it is safe either way; if a column already exists, the statement
  is a no-op. Cannot be confirmed without DB access (no Git / no live DB query performed).
- Whether other tables (beyond `jobs`/`approval_requests`) have undetected drift — the audit focused on the two
  tables implicated by the runtime errors plus a full `ApprovalRequest` model↔table diff. No other table was
  named in the runtime evidence.
- Supabase migrator transaction semantics (the file is standard additive DDL; the runner applies it in a single
  transaction per Supabase convention — not re-verified against a live runner).

---

## SUCCESS CRITERIA CHECK

- [x] Git never used.
- [x] Existing migrations untouched.
- [x] One new migration only (`20260715_schema_drift_fix.sql`).
- [x] No destructive SQL (only ADD COLUMN IF NOT EXISTS).
- [x] No guessed schema (every column/type justified by code).
- [x] Repository ↔ Database parity achieved for the 3 reported errors.
- [x] ApprovalRequest ↔ approval_requests parity achieved (updated_at added to both model and table).
- [x] jobs table ↔ runtime parity achieved (execution_cursor, interaction_state added).
- [x] Production-safe migration (idempotent, additive, no data loss).

**Confidence: HIGH (production-grade).**
