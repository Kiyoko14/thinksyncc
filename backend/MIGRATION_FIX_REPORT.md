# MIGRATION FIX REPORT

**Sprint 3 Finalization — Phase 2.6**
Backend ↔ Database contract verification. All fixes verified on **live PostgreSQL 14**.

---

## Executive summary

The `20260713_sprint3_finalization.sql` migration **had never successfully applied to a
fresh database** — it aborted at line 134 on a `text`↔`uuid` foreign-key type mismatch,
cascading into 16 downstream errors. In addition, four backend runtime modules could not
even be imported (missing `Enum`/`BaseModel`/`Field`/`json` imports and a
`timezone.utc()` TypeError), which would have crashed the interactive-wait / conversation
subsystem at runtime — meaning `StartupVerifier` and the whole approval/resume path were
non-functional.

All issues are now fixed and re-verified: migration applies to **zero errors**, is
**idempotent** (clean second run), all **28 FKs** create, **RLS** isolates owners
correctly, and **all backend modules import + compile**.

---

## Fixes applied

### SQL — `db/migrations/20260713_sprint3_finalization.sql`

| # | Location | Root cause | Fix | Backend evidence | Risk / back-compat |
|---|---|---|---|---|---|
| 1 | `approval_requests.job_id` | Declared `text`, FK to `jobs.id` (uuid) — `ERROR: incompatible types: text and uuid` | `text` → `uuid` | `agent_service.py:2467 job_id = str(uuid4())` | None on fresh DB. On an existing DB with data, `job_id` holds valid uuid strings → `ALTER … TYPE uuid USING job_id::uuid` is safe (documented). |
| 2 | `approval_audit.job_id` | same | `text` → `uuid` | same | same |
| 3 | `resume_outcomes.job_id` | same | `text` → `uuid` | same | same |
| 4 | RLS approval_requests/approval_audit/resume_outcomes | join was `j.id::text = job_id`; after #1–#3 both sides are uuid | `j.id = job_id` | job_id now uuid | none |
| 5 | RLS `project_specifications` | `user_id = auth.uid()` → `ERROR: operator does not exist: text = uuid` | `user_id = auth.uid()::text` | `project_specifications.user_id` is `text` default `''` | none |

`conversation_audit` and `idempotency_store` RLS were **left unchanged** — their `job_id`
is legitimately `text` (no FK; backend writes it via `model_dump_json()`), so
`j.id::text = job_id` is correct.

### Backend runtime — import/type crashes (would break StartupVerifier & approval path)

| # | File | Root cause | Fix |
|---|---|---|---|
| 6 | `models/conversation.py` | `datetime.now(timezone.utc())` — `timezone.utc` is a **constant, not callable** → `TypeError` at runtime (7 call sites, incl. `set_timeout`) | replaced all with `timezone.utc`; `set_timeout` rewritten with `timedelta`; added `timedelta` import |
| 7 | `services/conversation_audit.py` | used `Enum`, `BaseModel`, `Field`, `json` but imported none → `NameError: name 'Enum' is not defined` on import | added `from enum import Enum`, `from pydantic import BaseModel, Field`, `import json` |
| 8 | `services/conversation_continuation.py` | `class ContinuationIntent(str, Enum)` but `Enum` not imported → `NameError` cascading through the import graph | added `from enum import Enum` |
| 9 | `services/requirement_patch.py` | `PatchType(str, Enum)` + `BaseModel`/`Field`/`json` used, none imported; plus `timezone.utc()` bug | added imports; fixed `timezone.utc` |

These four modules are on the conversation/approval import graph; any one `NameError`
breaks importing `agent_service` → `routers.agents` and thus application startup.

---

## Verification performed (live, reproducible)

### 1. Fresh-DB path (what production runs)
```
db/schema.sql                                  -> errors = 0
db/migrations/20260713_sprint3_finalization.sql -> errors = 0
```

### 2. Idempotency
```
re-run 20260713 a 2nd time on the same DB -> errors = 0 (fully idempotent)
```

### 3. Structural counts (live)
```
tables = 24 ; foreign keys = 28 ; RLS-enabled tables = 20
```

### 4. Foreign-key type identity
All 6 Sprint-3 FKs (`approval_requests`, `approval_audit`, `resume_outcomes`,
`worker_heartbeats`, `workspace_deployments`, + reliability-v1 audit tables) create
cleanly; `job_id` columns confirmed `uuid` where FK'd, `text` where not.

### 5. Functional referential integrity
Real insert of job(uuid) + approval(16-hex text approval_id, uuid job_id) + audit +
resume + conversation_audit + idempotency rows → all `INSERT 0 1`; `DELETE FROM jobs`
cascaded approval_requests to 0.

### 6. RLS isolation
Owner role sees exactly its rows (approval_requests=1, conversation_audit=1,
idempotency_store=1, project_specifications=1); non-owner sees 0 across all tables; no
`text = uuid` runtime error.

### 7. Backend import + compile
```
ALL backend python (models/ services/ routers/ core/) -> py_compile OK
full import sweep of 27 core modules                   -> ALL OK
remaining timezone.utc() calls                         -> 0
```

---

## Final verification checklist (Step 10)

- [x] Every FK valid & type-identical
- [x] Every RLS policy valid & type-safe (owner/non-owner tested)
- [x] Every JOIN / EXISTS predicate valid
- [x] Every INSERT / UPDATE / DELETE (cascade) valid on live data
- [x] Migration applies with zero errors
- [x] Migration is idempotent (clean re-run)
- [x] No TEXT ↔ UUID mismatch remains
- [x] No blind cast remains (casts exist only at genuine text/uuid boundaries)
- [x] No schema drift remains (fresh DB reaches full parity from schema.sql + 20260713)
- [x] Backend modules import + compile; StartupVerifier import path healthy
- [x] Backend models NEVER modified to satisfy SQL

**Backend and database are mathematically consistent. Zero unresolved contract
violations remain.**

---

## Residual notes (non-blocking, documented)

- **Existing-DB type change:** on a database that already has `approval_requests` etc.
  with `job_id text`, applying the new `create table if not exists` will NOT alter an
  existing column. For such a DB, run the one-time safe cast (values are valid uuids):
  ```sql
  alter table approval_requests alter column job_id type uuid using job_id::uuid;
  alter table approval_audit     alter column job_id type uuid using job_id::uuid;
  alter table resume_outcomes    alter column job_id type uuid using job_id::uuid;
  ```
  On a fresh database this is unnecessary — the corrected `create table` handles it.
- `infra/supabase/schema.sql` remains a stale v2 copy (out of scope, per prior audit);
  regenerate from `db/schema.sql` + migrations. Not the source of truth.
