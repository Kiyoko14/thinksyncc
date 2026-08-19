# BACKEND ↔ DATABASE CONTRACT

**Sprint 3 Finalization — Phase 2.6: Backend ↔ Database Contract Verification**
Date: 2026-07-13
Method: **Live PostgreSQL 14 replay** (not static analysis) — every migration was
executed against a real Postgres cluster with Supabase-style `auth.users` + `auth.uid()`
stubs; every error below is a real PostgreSQL error, and every fix was re-verified by
re-running the migration to a zero-error, idempotent state.

Rule applied throughout: **the backend is the single source of truth. The database
conforms to the backend. Backend models were NEVER modified to satisfy SQL.**

---

## Verification harness (reproducible)

```
DB:        thinksync_verify  (PostgreSQL 14, live cluster on :5432)
auth stub: create schema auth; auth.users(id uuid, email text);
           auth.uid() returns uuid  (owner / non-owner overridden per test)
Path 1 (fresh DB, what production runs): db/schema.sql  ->  db/migrations/20260713_sprint3_finalization.sql
Path 2 (historical replay): all db/migrations/*.sql + SPRINT_0*_MIGRATION.sql in date order
```

Final result: **schema.sql errors = 0 ; finalization errors = 0 ; idempotent re-run = 0 ;
24 tables ; 28 foreign keys ; 20 RLS-protected tables.**

---

## Table contract (backend-defined truth)

Every table below is written or read by the backend via the Supabase client
(`get_supabase().table("<name>")`). Types are proven from the Pydantic model and/or
the value the backend actually writes.

### `jobs` — core execution unit
Backend truth: `agent_service.py:2467` writes `job_id = str(uuid4())` → **full UUID**.

| column | type | null | default | notes / backend evidence |
|---|---|---|---|---|
| id | uuid | no | gen_random_uuid() | PK. `str(uuid4())` |
| user_id | uuid | no | — | FK auth.users(id) |
| workspace_id | uuid | yes | — | FK workspaces(id) |
| server_id | uuid | no | — | FK servers(id) |
| objective | text | no | — | |
| status | text | no | 'queued' | CHECK widened to 14 states (see below) |
| allow_write / dry_run | boolean | no | false | |
| task_mode | text | no | 'complex' | CHECK (simple\|complex) |
| plan/steps/decisions/errors/retries | jsonb | no | '[]' | |
| summary | text | yes | — | |
| intent | text | — | 'chat' | migration 20260413_jobs_intent |
| worker_id | text | yes | — | reliability v2 |
| claimed_at/heartbeat_at/completed_at | timestamptz | yes | — | reliability v2 |
| deleted_at | timestamptz | yes | — | soft-delete |
| recoverable | boolean | no | false | |
| recovery_reason | text | yes | — | |
| interaction_state/execution_cursor/clarification_session | jsonb | — | '{}' | SPRINT_03 |
| **conversation_id** | text | yes | — | added 20260713; `ConversationSession.conversation_id` |
| **conversation_session** | jsonb | yes | — | added 20260713; `ConversationSessionStore._save` |
| **cursor_version** | integer | no | 0 | added 20260713; `ExecutionCursor.cursor_version` |
| **spec** | jsonb | yes | — | added 20260713 |
| created_at/updated_at | timestamptz | no | now() | trigger-maintained |

### `approval_requests` — `models/approval.py::ApprovalRequest`
Backend truth: `approval_id = uuid4().hex[:16]` → **16-char hex, NOT a UUID** → stays `text`.
`job_id` comes from `jobs.id` → **must be `uuid`**.

| column | type | null | default | evidence |
|---|---|---|---|---|
| approval_id | **text** | no | — | PK; `uuid4().hex[:16]` (not a uuid) |
| **job_id** | **uuid** | no | — | **FIXED text→uuid**; FK jobs(id) |
| conversation_id | text | no | — | |
| approval_type | text | no | — | ApprovalType enum |
| status | text | no | 'pending' | ApprovalStatus enum |
| title/description/risk_level/resolved_by/reason | text | — | defaults | |
| affected_files/affected_commands/affected_assumptions/context | jsonb | — | defaults | |
| created_at/resolved_at | timestamptz | — | now()/null | |
| decision | text | yes | — | ApprovalDecision enum |
| spec_version/requirement_version | integer | yes | — | |
| request_version | integer | no | 0 | optimistic lock (Sprint 3B.1) |
| resume_token | jsonb | yes | — | `ResumeTokenStore` writes `model_dump_json()` |

### `approval_audit` — `models/approval.py::ApprovalAuditEvent`
| column | type | null | evidence |
|---|---|---|---|
| event_id | text | no | PK; `uuid4().hex[:16]` |
| approval_id | text | no | FK approval_requests(approval_id) |
| **job_id** | **uuid** | no | **FIXED text→uuid**; FK jobs(id) |
| conversation_id/event_type/decision/reason/"user" | text | — | |
| timestamp | timestamptz | — | |
| metadata | jsonb | — | |

### `resume_outcomes` — `conversation_reliability.py::ExactlyOnceResumeGuard.record_resume`
| column | type | null | evidence |
|---|---|---|---|
| id | uuid | no | PK gen_random_uuid() |
| approval_id | text | no | `uuid4().hex[:16]` |
| **job_id** | **uuid** | no | **FIXED text→uuid**; FK jobs(id); writes `job_id` from jobs |
| resume_result | jsonb | no | `json.dumps(..., default=str)` |
| created_at | timestamptz | no | |

### `conversation_audit` — `models/`+`services/conversation_audit.py::AuditEvent`
`job_id` here is written via `AuditEvent.model_dump_json()`; **no FK** to jobs, so it
stays `text` (stores the UUID as text). RLS compares `jobs.id::text = conversation_audit.job_id`.

| column | type | null | evidence |
|---|---|---|---|
| event_id | text | no | PK; `uuid4().hex[:16]` |
| job_id | text | no | no FK — text; RLS uses `j.id::text` |
| conversation_id/session_id/event_type/actor | text | — | |
| content | jsonb | no | AuditEvent.content |
| spec_version/cursor_version | integer | yes | |
| timestamp | timestamptz | no | |

### `idempotency_store` — `conversation_reliability.py::IdempotencyStore.record`
| column | type | null | evidence |
|---|---|---|---|
| id | uuid | no | PK |
| operation_id | text | no | UNIQUE(operation_id, job_id) |
| job_id | text | no | no FK — text; RLS uses `j.id::text` |
| result | jsonb | no | |
| created_at | timestamptz | no | |

### Other backend tables (verified, unchanged)
`servers, workspaces, chats, messages, chat_messages, tasks, job_state_transitions,
job_events, job_steps, job_decisions, job_retries, job_execution_details,
workspace_files, agent_context_logs, agent_runs, worker_heartbeats,
workspace_deployments, project_specifications`.
All `job_id` columns on the reliability-v1 audit tables are `uuid` with a proper FK to
`jobs(id)` — verified type-identical (see FOREIGN_KEY_VERIFICATION.md).

---

## ID contract (Step 2 — traced end-to-end)

| identifier | Python origin | Python type | serialized | DB type | proven at |
|---|---|---|---|---|---|
| job_id / jobs.id | `str(uuid4())` | str (uuid form) | uuid string | **uuid** | agent_service.py:2467 |
| approval_id | `uuid4().hex[:16]` | str (16 hex) | text | **text** | approval.py:140 |
| event_id (audit) | `uuid4().hex[:16]` | str (16 hex) | text | **text** | approval.py:182, conversation_audit.py:56 |
| conversation_id | caller-supplied string | str | text | **text** | JobCreate.conversation_id |
| session_id | `uuid4().hex[:16]` | str | text | **text** | conversation.py:57 |
| workspace_id / server_id / user_id | Supabase uuid | str | uuid string | **uuid** | schema.sql |

**Key insight that resolves the whole drift:** `job_id` is a full `uuid4()` but
`approval_id`/`event_id`/`session_id` are truncated `uuid4().hex[:16]`. The original
Sprint-3 migration wrongly typed ALL of them `text`. Only the truncated-hex identifiers
are legitimately `text`; `job_id` must be `uuid` to FK into `jobs`.

---

## Status enum contract (`jobs.status`)

Backend writes (proven from `JobStatus` + worker/reliability code):
`queued, claimed, running, waiting_for_llm, waiting_for_user, approved, rejected,
resumed, paused, cancelled, completed, failed, abandoned, recoverable` — **14 values**.
The `20260713` migration widens the CHECK to exactly this superset inside a `DO $$`
block (non-destructive; old rows keep their value). Verified applied.

---

## Conclusion

After the Phase 2.6 fixes the backend and database are **mathematically consistent**:
every table the backend touches exists, every column it writes exists with the correct
type, every FK is creatable, every RLS join is type-safe, and the migration is
idempotent. Zero unresolved contract violations remain.
