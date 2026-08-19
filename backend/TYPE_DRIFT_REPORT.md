# TYPE DRIFT REPORT

**Phase 2.6 — Backend ↔ Database type-drift detection**
Every row below was confirmed by a **real PostgreSQL error** during live migration
replay, not inferred statically.

## Summary

| # | Location | Backend | SQL (before) | Correct | Reason | Status |
|---|---|---|---|---|---|---|
| 1 | `approval_requests.job_id` | uuid (`str(uuid4())`) | `text` | **uuid** | FK to `jobs.id` (uuid) — Postgres rejects text→uuid FK | ✅ FIXED |
| 2 | `approval_audit.job_id` | uuid | `text` | **uuid** | same FK type-identity rule | ✅ FIXED |
| 3 | `resume_outcomes.job_id` | uuid | `text` | **uuid** | same FK type-identity rule | ✅ FIXED |
| 4 | `project_specifications` RLS `user_id = auth.uid()` | user_id is `text` (default `''`) | `text = uuid` | `user_id = auth.uid()::text` | text vs uuid operator does not exist | ✅ FIXED |
| 5 | `approval_requests`/`approval_audit`/`resume_outcomes` RLS join | job_id now uuid | `j.id::text = job_id` | `j.id = job_id` | after #1–#3 both sides are uuid; the `::text` cast was only needed while job_id was text | ✅ FIXED |
| 6 | `conversation_audit.job_id` RLS join | job_id is `text` (no FK) | `j.id::text = job_id` | *(unchanged — correct)* | legitimately text; cast is required and correct | ✅ OK |
| 7 | `idempotency_store.job_id` RLS join | job_id is `text` (no FK) | `j.id::text = job_id` | *(unchanged — correct)* | legitimately text; cast is required and correct | ✅ OK |

**No** JSON↔JSONB, TIMESTAMP↔TIMESTAMPTZ, BOOLEAN↔TEXT, or ARRAY↔JSON drift was found.
All jsonb columns are `jsonb` (never `json`); all timestamps are `timestamptz`; all
booleans are `boolean`; all list/array fields are stored as `jsonb`. The ONLY drift
class present was **UUID ↔ TEXT on `job_id`**, plus its downstream RLS cast mismatch.

---

## Root-cause evidence (real PostgreSQL errors)

### Drift #1 — the primary error that blocked the entire migration
```
psql: 20260713_sprint3_finalization.sql:134: ERROR:  foreign key constraint
  "approval_requests_job_id_fkey" cannot be implemented
DETAIL:  Key columns "job_id" and "id" are of incompatible types: text and uuid.
```
Because the migration aborts at line 134 (ON_ERROR_STOP), **every subsequent statement
also failed** with `relation "public.approval_requests" does not exist` — a cascade of
16 errors all rooted in this single type mismatch. This means the Sprint-3 finalization
migration had **never successfully applied** on a fresh database.

### Drift #4 — RLS text=uuid
```
psql: 20260713_sprint3_finalization.sql:437: ERROR:  operator does not exist: text = uuid
```
`project_specifications.user_id` is `text` (backend default `''`), but `auth.uid()`
returns `uuid`. `text = uuid` has no implicit operator in PostgreSQL.

---

## Why the backend was NOT changed

- `approval_id`, `event_id`, `session_id` are `uuid4().hex[:16]` (16-char strings, **not**
  valid UUIDs) → they are correctly `text`; forcing them to `uuid` would break inserts.
- `job_id` is a full `str(uuid4())` → it is a valid UUID and MUST be `uuid` to satisfy the
  FK the backend's data model implies (approval → job ownership + cascade delete).
- `project_specifications.user_id` is deliberately `text` (default `''`, no FK) → the SQL
  side is cast (`auth.uid()::text`) rather than changing the model.

The fix direction is always **SQL conforms to backend**, per the task contract.
